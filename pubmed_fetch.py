#!/usr/bin/env python3
"""
PubMed Search Utility

Search PubMed (NCBI E-utilities) for articles matching MeSH terms, authors and/or
organizations within a publication-date window. Results are saved as JSON, JSONL or CSV
with full abstracts and full-text availability from PubMed Central; open-access
full text can optionally be downloaded.

Usage:
    uv run pubmed_fetch.py --mesh-terms "Endometrial Neoplasms" "Ovarian Neoplasms" --days 30
    uv run pubmed_fetch.py --author "Smith J" --start-date 2024-01-01 --end-date 2024-12-31 --output smith.csv
    uv run pubmed_fetch.py --config config.ini
"""

import argparse
import configparser
import csv
import json
import logging
import os
import sys
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

log = logging.getLogger("pubmed_fetch")

EUTILS_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"
PMC_S3_BUCKET_URL = "https://pmc-oa-opendata.s3.amazonaws.com/"
S3_NS = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}
# PubMed's E-utilities will not return records beyond the first 10,000 of a search.
PUBMED_RECORD_LIMIT = 10_000

DEFAULTS = {
    "mesh_terms": [],
    "authors": [],
    "organizations": [],
    "days": 30,
    "start_date": None,
    "end_date": None,
    "max_results": 1000,
    "output_file": None,
    "format": None,
    "email": None,
    "api_key": None,
    "download_dir": None,
    "download_formats": ["pdf"],
}

OUTPUT_FORMATS = ("json", "jsonl", "csv")
DOWNLOAD_FORMATS = ("pdf", "xml", "txt")
CSV_FIELDS = [
    "pmid",
    "title",
    "authors",
    "journal",
    "date",
    "doi",
    "pmc_id",
    "url",
    "mesh_terms",
    "keywords",
    "publication_types",
    "fulltext_url",
    "access_type",
    "license",
    "abstract",
]

MONTHS = {
    m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)
}


class PubMedError(Exception):
    """Raised when PubMed cannot be queried or returns an error."""


def _text(elem):
    """Return all text inside an element, including text in inline markup such as <i> or <sup>."""
    return "".join(elem.itertext()).strip() if elem is not None else ""


def _make_session(pool_size=10):
    retry = Retry(
        total=4,
        backoff_factor=1,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET", "POST"}),
        respect_retry_after_header=True,
    )
    session = requests.Session()
    session.mount("https://", HTTPAdapter(max_retries=retry, pool_maxsize=pool_size))
    return session


def _quote(term):
    return '"' + term.replace('"', "").strip() + '"'


def build_query(mesh_terms=None, authors=None, organizations=None):
    """Build a PubMed query: OR within each category, AND across categories."""
    groups = [
        (mesh_terms, "MeSH Terms"),
        (authors, "Author"),
        (organizations, "Affiliation"),
    ]
    subqueries = ["(" + " OR ".join(f"{_quote(t)}[{tag}]" for t in terms) + ")" for terms, tag in groups if terms]
    return " AND ".join(subqueries)


def resolve_date_range(days_back=30, start_date=None, end_date=None, today=None):
    """
    Return (start, end) dates. Explicit dates win; a missing end defaults to today and a
    missing start defaults to ``days_back`` days before the end.
    """
    end = end_date or today or date.today()
    start = start_date or end - timedelta(days=days_back)
    if start > end:
        raise PubMedError(f"Start date {start} is after end date {end}.")
    return start, end


def resolve_output(filename=None, fmt=None):
    """Return (filename, format). The format comes from ``fmt``, else the file extension, else JSON."""
    ext_fmt = {f".{f}": f for f in OUTPUT_FORMATS}.get(Path(filename).suffix.lower()) if filename else None
    fmt = fmt or ext_fmt or "json"
    return filename or f"pubmed_articles.{fmt}", fmt


def _csv_row(article):
    fulltext = article.get("fulltext") or {}
    row = {key: article.get(key, "") for key in CSV_FIELDS}
    for key in ("authors", "mesh_terms", "keywords", "publication_types"):
        row[key] = "; ".join(article.get(key) or [])
    row["fulltext_url"] = fulltext.get("url", "")
    row["access_type"] = fulltext.get("access_type", "")
    row["license"] = fulltext.get("license", "")
    return row


def _normalize_pmcid(pmc_id):
    return pmc_id if pmc_id.upper().startswith("PMC") else f"PMC{pmc_id}"


def _s3_to_https(url):
    """Convert an s3://pmc-oa-opendata/... URL into a public HTTPS URL."""
    path = url.split("?", 1)[0].removeprefix("s3://pmc-oa-opendata/")
    return PMC_S3_BUCKET_URL + path


def _format_pub_date(pub_date):
    if pub_date is None:
        return "N/A"
    year = _text(pub_date.find("Year"))
    if not year:
        return _text(pub_date.find("MedlineDate")) or "N/A"
    month = _text(pub_date.find("Month"))
    month_num = MONTHS.get(month[:3].lower()) or (int(month) if month.isdigit() else None)
    if not month_num:
        return year
    day = _text(pub_date.find("Day"))
    return f"{year}-{month_num:02d}" + (f"-{int(day):02d}" if day.isdigit() else "")


def _author_name(author):
    last, fore = _text(author.find("LastName")), _text(author.find("ForeName"))
    if last:
        return f"{last}, {fore}" if fore else last
    return _text(author.find("CollectiveName"))


class PubMedSearcher:
    batch_size = 200
    fulltext_workers = 8

    def __init__(self, email=None, api_key=None, tool="pubmed-fetch", session=None, timeout=30):
        self.base_url = EUTILS_URL
        self.tool = tool
        self.email = email
        self.api_key = api_key
        self.timeout = timeout
        self.session = session or _make_session(self.fulltext_workers)
        self.search_info = {}
        self._last_request = 0.0

    @property
    def min_interval(self):
        # NCBI allows 3 requests/second without an API key and 10/second with one.
        return 0.1 if self.api_key else 0.34

    def _eutils(self, endpoint, params):
        """Make a rate-limited E-utilities request and return the parsed XML root."""
        params = {**params, "tool": self.tool}
        if self.email:
            params["email"] = self.email
        if self.api_key:
            params["api_key"] = self.api_key

        wait = self._last_request + self.min_interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        try:
            response = self.session.post(f"{self.base_url}{endpoint}", data=params, timeout=self.timeout)
        finally:
            self._last_request = time.monotonic()
        response.raise_for_status()

        root = ET.fromstring(response.content)
        error = root.find("ERROR")
        if error is not None:
            raise PubMedError(_text(error))
        return root

    def search_pubmed(
        self,
        mesh_terms=None,
        authors=None,
        organizations=None,
        days_back=30,
        max_results=1000,
        start_date=None,
        end_date=None,
    ):
        """
        Search PubMed for articles using any combination of:
        - MeSH terms (list of strings)
        - authors (list of strings; matched with [Author])
        - organizations/affiliations (list of strings; matched with [Affiliation])
        and restrict to a publication date window: ``start_date``..``end_date`` (dates), or the
        ``days_back`` days before ``end_date`` (default today) when ``start_date`` is not given.

        Returns a list of article dicts. Details of the search are stored in ``self.search_info``.
        Raises PubMedError if the search itself fails.
        """
        search_term = build_query(mesh_terms, authors, organizations)
        if not search_term:
            raise PubMedError(
                "No search terms provided. Specify at least one of --mesh-terms, --author, --organization."
            )

        start_date, end_date = resolve_date_range(days_back, start_date, end_date)

        log.info("Search query: %s", search_term)
        log.info("Date range: %s to %s", start_date.isoformat(), end_date.isoformat())

        try:
            root = self._eutils(
                "esearch.fcgi",
                {
                    "db": "pubmed",
                    "term": search_term,
                    "datetype": "pdat",
                    "mindate": start_date.strftime("%Y/%m/%d"),
                    "maxdate": end_date.strftime("%Y/%m/%d"),
                    "usehistory": "y",
                    "retmax": 0,
                },
            )
        except (requests.RequestException, ET.ParseError) as e:
            raise PubMedError(f"Error searching PubMed: {e}") from e

        total_count = int(_text(root.find("Count")) or 0)
        webenv, query_key = _text(root.find("WebEnv")), _text(root.find("QueryKey"))

        self.search_info = {
            "query": search_term,
            "mesh_terms": list(mesh_terms or []),
            "authors": list(authors or []),
            "organizations": list(organizations or []),
            "date_range": {"start_date": start_date.isoformat(), "end_date": end_date.isoformat()},
            "total_found": total_count,
        }

        log.info("Found %d articles", total_count)
        to_fetch = min(total_count, max_results, PUBMED_RECORD_LIMIT)
        if to_fetch < total_count:
            log.warning(
                "Only the first %d of %d matching articles will be retrieved "
                "(raise --max-results or narrow the search to get the rest; PubMed's limit is %d).",
                to_fetch,
                total_count,
                PUBMED_RECORD_LIMIT,
            )
        if not to_fetch:
            return []
        return self._efetch_batches(
            [
                {
                    "WebEnv": webenv,
                    "query_key": query_key,
                    "retstart": start,
                    "retmax": min(self.batch_size, to_fetch - start),
                }
                for start in range(0, to_fetch, self.batch_size)
            ]
        )

    def fetch_article_details(self, pmids):
        """Fetch detailed information for a list of PMIDs, in batches."""
        return self._efetch_batches(
            [{"id": ",".join(pmids[i : i + self.batch_size])} for i in range(0, len(pmids), self.batch_size)]
        )

    def _efetch_batches(self, batches, attempts=2):
        articles, failed = [], 0
        for num, params in enumerate(batches, 1):
            log.info("Fetching batch %d/%d...", num, len(batches))
            # The history server intermittently answers 400 for a valid WebEnv, so retry once more.
            for attempt in range(1, attempts + 1):
                try:
                    root = self._eutils("efetch.fcgi", {"db": "pubmed", "retmode": "xml", **params})
                    break
                except (requests.RequestException, ET.ParseError, PubMedError) as e:
                    error = e
                    if attempt < attempts:
                        time.sleep(2)
            else:
                failed += 1
                log.warning("Batch %d/%d failed and was skipped: %s", num, len(batches), error)
                continue
            articles.extend(a for a in map(self.parse_article, root.iter("PubmedArticle")) if a)
        if batches and failed == len(batches):
            raise PubMedError(f"Could not fetch article details from PubMed: {error}")
        log.info("Total articles retrieved: %d", len(articles))
        return articles

    def parse_article(self, article_xml):
        """Parse a <PubmedArticle> element into a dict."""
        citation = article_xml.find("MedlineCitation")
        article = citation.find("Article") if citation is not None else None
        if article is None:
            log.warning("Skipping malformed PubmedArticle record")
            return None

        pmid = _text(citation.find("PMID")) or "N/A"
        authors = [name for a in article.findall("AuthorList/Author") if (name := _author_name(a))]
        author_str = "; ".join(authors[:5]) + (" et al." if len(authors) > 5 else "")

        abstract_parts = []
        for elem in article.findall("Abstract/AbstractText"):
            label, text = elem.get("Label", ""), _text(elem)
            abstract_parts.append(f"{label}: {text}" if label else text)

        # Only use the article's own IDs, not those of cited references in PubmedData/ReferenceList.
        ids = {}
        for id_elem in article_xml.findall("PubmedData/ArticleIdList/ArticleId"):
            ids.setdefault(id_elem.get("IdType"), _text(id_elem))
        doi = ids.get("doi") or next(
            (_text(e) for e in article.findall("ELocationID") if e.get("EIdType") == "doi"),
            "",
        )

        return {
            "pmid": pmid,
            "title": _text(article.find("ArticleTitle")) or "N/A",
            "authors": authors,
            "authors_display": author_str,
            "journal": _text(article.find("Journal/Title")) or "N/A",
            "date": _format_pub_date(article.find("Journal/JournalIssue/PubDate")),
            "abstract": " ".join(p for p in abstract_parts if p),
            "mesh_terms": [_text(d) for d in citation.findall("MeshHeadingList/MeshHeading/DescriptorName")],
            "keywords": [_text(k) for k in citation.findall("KeywordList/Keyword") if _text(k)],
            "publication_types": [_text(p) for p in article.findall("PublicationTypeList/PublicationType")],
            "doi": doi,
            "pmc_id": ids.get("pmc", ""),
            "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
            "fulltext": None,
        }

    def add_fulltext_info(self, articles):
        """
        Add full-text availability information.

        Articles in PubMed Central are free to read ("free"). Those in the PMC Open Access
        Subset ("open") are identified, with license and download links, through the PMC
        Cloud Service on AWS. Other articles get a DOI link with unknown access.
        """
        if not articles:
            return articles

        log.info("Checking full-text availability for %d articles...", len(articles))
        pmc_articles = []
        for article in articles:
            article["fulltext"] = None
            if article.get("pmc_id"):
                pmcid = _normalize_pmcid(article["pmc_id"])
                article["fulltext"] = {
                    "url": f"https://pmc.ncbi.nlm.nih.gov/articles/{pmcid}/",
                    "source": "PMC",
                    "access_type": "free",
                }
                pmc_articles.append(article)
            elif article.get("doi"):
                article["fulltext"] = {
                    "url": f"https://doi.org/{article['doi']}",
                    "source": "Publisher (via DOI)",
                    "access_type": "unknown",
                }

        failures = 0
        with ThreadPoolExecutor(max_workers=self.fulltext_workers) as pool:
            pmcids = [_normalize_pmcid(a["pmc_id"]) for a in pmc_articles]
            for article, result in zip(pmc_articles, pool.map(self._pmc_dataset_metadata, pmcids), strict=True):
                if isinstance(result, Exception):
                    failures += 1
                    log.debug("PMC dataset lookup failed for %s: %s", article["pmc_id"], result)
                elif result:
                    self._apply_pmc_metadata(article["fulltext"], result)
        if failures:
            log.warning("Could not check open-access status for %d PMC articles.", failures)

        fulltext_count = sum(1 for a in articles if a["fulltext"])
        open_count = sum(1 for a in articles if a["fulltext"] and a["fulltext"]["access_type"] == "open")
        log.info("Full-text links found for %d articles (%d open access).", fulltext_count, open_count)
        return articles

    def _pmc_dataset_metadata(self, pmcid):
        """
        Look up an article in the PMC Article Datasets (S3) and return its JSON metadata,
        or None if it is not part of the datasets. Exceptions are returned, not raised.
        """
        try:
            response = self.session.get(
                PMC_S3_BUCKET_URL,
                params={"list-type": "2", "prefix": f"{pmcid}.", "delimiter": "/"},
                timeout=self.timeout,
            )
            response.raise_for_status()
            root = ET.fromstring(response.content)
            versions = [_text(p).rstrip("/") for p in root.findall("s3:CommonPrefixes/s3:Prefix", S3_NS)]
            metadata = []
            for version in versions:
                meta_response = self.session.get(f"{PMC_S3_BUCKET_URL}metadata/{version}.json", timeout=self.timeout)
                meta_response.raise_for_status()
                metadata.append(meta_response.json())
        except (requests.RequestException, ET.ParseError, ValueError) as e:
            return e
        if not metadata:
            return None
        # Prefer an open-access, final published version; otherwise the latest version.
        return max(
            metadata,
            key=lambda m: (bool(m.get("is_pmc_openaccess")), not m.get("is_manuscript"), m.get("version") or 0),
        )

    @staticmethod
    def _apply_pmc_metadata(fulltext, meta):
        fulltext["access_type"] = "open" if meta.get("is_pmc_openaccess") else "free"
        if meta.get("license_code"):
            fulltext["license"] = meta["license_code"]
        if meta.get("is_retracted"):
            fulltext["retracted"] = True
        links = [
            {"format": fmt, "url": _s3_to_https(meta[key])}
            for fmt, key in (("pdf", "pdf_url"), ("xml", "xml_url"), ("txt", "text_url"))
            if meta.get(key)
        ]
        if links:
            fulltext["download_links"] = links

    def display_results(self, articles, out=None):
        """Print search results in a human-readable format."""
        out = out or sys.stdout
        if not articles:
            print("No articles found.", file=out)
            return

        print(f"\nRetrieved {len(articles)} articles", file=out)
        print("=" * 80, file=out)
        for i, article in enumerate(articles, 1):
            lines = [
                f"\n{i}. {article['title']}",
                f"   Authors: {article['authors_display']}",
                f"   Journal: {article['journal']}",
                f"   Date: {article['date']}",
                f"   PMID: {article['pmid']}",
                f"   URL: {article['url']}",
            ]
            if article["mesh_terms"]:
                suffix = "..." if len(article["mesh_terms"]) > 5 else ""
                lines.append(f"   MeSH Terms: {', '.join(article['mesh_terms'][:5])}{suffix}")
            if article["abstract"]:
                abstract = article["abstract"]
                lines.append(f"   Abstract: {abstract[:200] + '...' if len(abstract) > 200 else abstract}")
            if article.get("pmc_id"):
                lines.append(f"   PMC ID: {article['pmc_id']}")
            fulltext = article.get("fulltext")
            if fulltext:
                indicator = {
                    "open": f" [Open Access{', ' + fulltext['license'] if fulltext.get('license') else ''}]",
                    "free": " [Free in PMC]",
                }.get(fulltext["access_type"], "")
                if fulltext.get("retracted"):
                    indicator += " [RETRACTED]"
                lines.append(f"   Full-text: {fulltext['url']}{indicator}")
                if fulltext.get("download_links"):
                    formats = ", ".join(link["format"] for link in fulltext["download_links"])
                    lines.append(f"   Downloads: {formats} available")
            lines.append("-" * 40)
            print("\n".join(lines), file=out)

    def save_to_json(self, articles, filename="pubmed_articles.json"):
        """Save results, together with details of the search, to a JSON file."""
        data = {
            "search_info": {
                **self.search_info,
                "generated_on": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "total_articles": len(articles),
            },
            "articles": articles,
        }
        with open(filename, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        log.info("Results saved to: %s", filename)

    def save_results(self, articles, filename=None, fmt=None):
        """Save results as JSON (with search_info), JSONL (one article per line) or CSV."""
        filename, fmt = resolve_output(filename, fmt)
        if fmt == "json":
            return self.save_to_json(articles, filename)
        with open(filename, "w", encoding="utf-8", newline="") as f:
            if fmt == "jsonl":
                f.writelines(json.dumps(article, ensure_ascii=False) + "\n" for article in articles)
            elif fmt == "csv":
                writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
                writer.writeheader()
                writer.writerows(map(_csv_row, articles))
            else:
                raise ValueError(f"Unknown output format: {fmt}")
        log.info("Results saved to: %s", filename)

    def download_fulltext(self, articles, directory, formats=("pdf",)):
        """
        Download available full-text files (from ``fulltext.download_links``) into ``directory``.
        Existing files are skipped. Each downloaded link gets a ``path`` entry.
        Returns (downloaded, failed) counts.
        """
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        links = [
            link
            for article in articles
            for link in ((article.get("fulltext") or {}).get("download_links") or [])
            if link["format"] in formats
        ]
        if not links:
            log.info("No downloadable full-text files (%s) found.", ", ".join(formats))
            return 0, 0

        log.info("Downloading %d full-text files to %s...", len(links), directory)
        with ThreadPoolExecutor(max_workers=self.fulltext_workers) as pool:
            results = list(pool.map(lambda link: self._download_file(link, directory), links))
        downloaded, failed = results.count(True), results.count(False)
        if failed:
            log.warning("Failed to download %d of %d files.", failed, len(links))
        log.info("Downloaded %d files (%d already present).", downloaded, results.count(None))
        return downloaded, failed

    def _download_file(self, link, directory):
        """Download one link. Returns True if downloaded, None if already present, False on failure."""
        dest = directory / link["url"].rsplit("/", 1)[-1]
        if dest.exists() and dest.stat().st_size:
            link["path"] = str(dest)
            return None
        partial = dest.with_name(dest.name + ".part")
        try:
            with self.session.get(link["url"], stream=True, timeout=self.timeout) as response:
                response.raise_for_status()
                with open(partial, "wb") as f:
                    for chunk in response.iter_content(chunk_size=1 << 16):
                        f.write(chunk)
            partial.replace(dest)
        except (requests.RequestException, OSError) as e:
            log.debug("Download failed for %s: %s", link["url"], e)
            partial.unlink(missing_ok=True)
            return False
        link["path"] = str(dest)
        return True


def _split_list(value):
    return [item.strip() for item in value.split(",") if item.strip()]


def load_config(config_file):
    """
    Load settings from the [search] section of an INI file.
    Only keys present in the file are returned, so callers can layer CLI > config > defaults.
    """
    config = configparser.ConfigParser()
    config.read(config_file)
    if "search" not in config:
        return {}
    section = config["search"]

    settings = {}
    for key in ("mesh_terms", "authors", "organizations"):
        if key in section:
            settings[key] = _split_list(section[key])
    for key in ("days", "max_results"):
        if key in section:
            settings[key] = section.getint(key)
    for key in ("output_file", "email", "api_key", "download_dir"):
        if section.get(key, "").strip():
            settings[key] = section[key].strip()
    for key in ("start_date", "end_date"):
        if section.get(key, "").strip():
            settings[key] = date.fromisoformat(section[key].strip())
    if section.get("format", "").strip():
        settings["format"] = _choice(section["format"], OUTPUT_FORMATS, "format")
    if "download_formats" in section:
        settings["download_formats"] = [
            _choice(f, DOWNLOAD_FORMATS, "download_formats") for f in _split_list(section["download_formats"])
        ]
    return settings


def _choice(value, choices, name):
    value = value.strip().lower()
    if value not in choices:
        raise ValueError(f"{name} must be one of {', '.join(choices)}, got {value!r}")
    return value


def _positive_int(value):
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError(f"must be a positive integer, got {value}")
    return number


def _iso_date(value):
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a date as YYYY-MM-DD, got {value!r}") from None


def parse_arguments(argv=None):
    """Parse command-line arguments. Unset options are None so config values can apply."""
    parser = argparse.ArgumentParser(
        description="Search PubMed for articles with specified filters (MeSH terms, authors, organizations)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s --mesh-terms "Endometrial Neoplasms" "Ovarian Neoplasms" --days 30
  %(prog)s --author "Gilbert P" --days 30
  %(prog)s --organization "Harvard Medical School" --days 365
  %(prog)s --mesh-terms "Breast Neoplasms" --author "Smith J" --days 7 --output results.json
  %(prog)s --author "Smith J" --start-date 2024-01-01 --end-date 2024-12-31 --output smith.csv
  %(prog)s --mesh-terms "Ovarian Neoplasms" --days 7 --download-fulltext papers/ --download-formats pdf xml
  %(prog)s --config config.ini

Environment variables:
  NCBI_EMAIL, NCBI_API_KEY  used when --email / --api-key are not given
        """,
    )
    parser.add_argument("--mesh-terms", nargs="+", help="MeSH terms to search for (space-separated)")
    parser.add_argument(
        "--author",
        nargs="+",
        help='Author name(s) to search for (e.g., "Gilbert P"). Space-separated for multiple.',
    )
    parser.add_argument(
        "--organization",
        nargs="+",
        help='Organization/Affiliation name(s) (e.g., "Harvard Medical School"). Space-separated for multiple.',
    )
    when = parser.add_mutually_exclusive_group()
    when.add_argument(
        "--days",
        type=_positive_int,
        help=f"Number of days before the end date to include (default: {DEFAULTS['days']})",
    )
    when.add_argument("--start-date", type=_iso_date, help="Earliest publication date (YYYY-MM-DD)")
    parser.add_argument("--end-date", type=_iso_date, help="Latest publication date (YYYY-MM-DD; default: today)")
    parser.add_argument(
        "--max-results",
        type=_positive_int,
        help=f"Maximum number of results to fetch (default: {DEFAULTS['max_results']}, PubMed limit: 10000)",
    )
    parser.add_argument(
        "--output",
        help="Output filename (default: pubmed_articles.<format>); a .json/.jsonl/.csv extension sets the format",
    )
    parser.add_argument(
        "--format",
        choices=OUTPUT_FORMATS,
        help="Output format: json (with search_info), jsonl (one article per line) or csv (default: json)",
    )
    parser.add_argument("--email", help="Your email address for NCBI API (recommended)")
    parser.add_argument("--api-key", help="NCBI API key; raises the rate limit from 3 to 10 requests/second")
    parser.add_argument("--no-fulltext", action="store_true", help="Skip the PMC full-text availability check")
    parser.add_argument(
        "--download-fulltext",
        metavar="DIR",
        help="Download available PMC full-text files (open access and author manuscripts) into DIR",
    )
    parser.add_argument(
        "--download-formats",
        nargs="+",
        choices=DOWNLOAD_FORMATS,
        help="File formats to download with --download-fulltext (default: pdf)",
    )
    parser.add_argument("--config", help="Configuration file (INI format)")
    parser.add_argument("--create-config", metavar="FILENAME", help="Create a sample configuration file and exit")
    verbosity = parser.add_mutually_exclusive_group()
    verbosity.add_argument("-q", "--quiet", action="store_true", help="Only print warnings and errors")
    verbosity.add_argument("-v", "--verbose", action="store_true", help="Print debug output")
    return parser.parse_args(argv)


SAMPLE_CONFIG = """[search]
# MeSH terms to search for (comma-separated)
mesh_terms = Endometrial Neoplasms, Ovarian Neoplasms

# Authors to filter by (comma-separated; optional)
#authors = Gilbert P, Smith J

# Organizations/Affiliations to filter by (comma-separated; optional)
#organizations = Harvard Medical School, National Institutes of Health

# Number of past days to include in search
days = 30

# Or an absolute publication date range (YYYY-MM-DD; start_date overrides days)
#start_date = 2024-01-01
#end_date = 2024-12-31

# Maximum number of results to fetch
max_results = 1000

# Output filename; the extension (.json, .jsonl, .csv) selects the format unless format is set
output_file = pubmed_articles.json
#format = csv

# Download available PMC full-text files into this directory (optional)
#download_dir = papers
#download_formats = pdf, xml

# Your email address (recommended for NCBI API)
email = user@example.com

# NCBI API key (optional; or set NCBI_API_KEY)
#api_key =
"""


def create_sample_config(filename):
    """Create a sample configuration file. Refuses to overwrite an existing file."""
    try:
        with open(filename, "x") as f:
            f.write(SAMPLE_CONFIG)
    except OSError as e:
        log.error("Error creating config file: %s", e)
        return False
    log.info("Sample configuration file created: %s", filename)
    log.info("Edit the file with your desired search parameters.")
    return True


def resolve_settings(args, config):
    """Merge settings with precedence: command line > environment > config file > defaults."""
    cli = {
        "mesh_terms": args.mesh_terms,
        "authors": args.author,
        "organizations": args.organization,
        "days": args.days,
        "start_date": args.start_date,
        "end_date": args.end_date,
        "max_results": args.max_results,
        "output_file": args.output,
        "format": args.format,
        "email": args.email,
        "api_key": args.api_key,
        "download_dir": args.download_fulltext,
        "download_formats": args.download_formats,
    }
    env = {"email": os.environ.get("NCBI_EMAIL"), "api_key": os.environ.get("NCBI_API_KEY")}
    settings = dict(DEFAULTS)
    for layer in (config, env, cli):
        settings.update({k: v for k, v in layer.items() if v is not None})
    # --days on the command line replaces a start_date from the config file.
    if args.days is not None and args.start_date is None:
        settings["start_date"] = None
    return settings


def main(argv=None):
    """Run the PubMed search. Returns a process exit code."""
    args = parse_arguments(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING if args.quiet else logging.INFO,
        format="%(message)s",
        stream=sys.stderr,
    )

    if args.create_config:
        return 0 if create_sample_config(args.create_config) else 1

    config = {}
    if args.config:
        if not os.path.exists(args.config):
            log.error("Error: Configuration file '%s' not found.", args.config)
            return 1
        try:
            config = load_config(args.config)
        except (configparser.Error, ValueError) as e:
            log.error("Error reading configuration file '%s': %s", args.config, e)
            return 1

    settings = resolve_settings(args, config)
    if not (settings["mesh_terms"] or settings["authors"] or settings["organizations"]):
        log.error(
            "Error: No search filters specified. "
            "Use --mesh-terms, --author, --organization, or provide a config file.\n"
            "Use --help for usage information or --create-config to create a sample config file."
        )
        return 1
    if settings["download_dir"] and args.no_fulltext:
        log.error("Error: --download-fulltext needs the full-text check; remove --no-fulltext.")
        return 1
    if not settings["email"]:
        log.warning("Tip: pass --email (or set NCBI_EMAIL) so NCBI can contact you about heavy usage.")

    searcher = PubMedSearcher(email=settings["email"], api_key=settings["api_key"])
    try:
        articles = searcher.search_pubmed(
            mesh_terms=settings["mesh_terms"],
            authors=settings["authors"],
            organizations=settings["organizations"],
            days_back=settings["days"],
            max_results=settings["max_results"],
            start_date=settings["start_date"],
            end_date=settings["end_date"],
        )
    except PubMedError as e:
        log.error("%s", e)
        return 1

    if articles and not args.no_fulltext:
        searcher.add_fulltext_info(articles)

    if articles and settings["download_dir"]:
        try:
            searcher.download_fulltext(articles, settings["download_dir"], settings["download_formats"])
        except OSError as e:
            log.error("Error creating download directory: %s", e)
            return 1

    if not args.quiet:
        searcher.display_results(articles)

    if articles:
        try:
            searcher.save_results(articles, filename=settings["output_file"], fmt=settings["format"])
        except OSError as e:
            log.error("Error saving to file: %s", e)
            return 1

    log.info("Search completed. Retrieved %d articles.", len(articles))
    return 0


def main_cli():
    sys.exit(main())


if __name__ == "__main__":
    main_cli()
