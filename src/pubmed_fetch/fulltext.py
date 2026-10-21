"""Full-text availability from PubMed Central (PMC Cloud Service on AWS) and file downloads."""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import requests

from .parse import Article, text

log = logging.getLogger(__name__)

PMC_S3_BUCKET_URL = "https://pmc-oa-opendata.s3.amazonaws.com/"
S3_NS = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}
DOWNLOAD_FORMATS = ("pdf", "xml", "txt")


def normalize_pmcid(pmc_id: str) -> str:
    return pmc_id if pmc_id.upper().startswith("PMC") else f"PMC{pmc_id}"


def s3_to_https(url: str) -> str:
    """Convert an s3://pmc-oa-opendata/... URL into a public HTTPS URL."""
    path = url.split("?", 1)[0].removeprefix("s3://pmc-oa-opendata/")
    return PMC_S3_BUCKET_URL + path


class PMCFullText:
    """
    Articles in PubMed Central are free to read ("free"). Those in the PMC Open Access Subset
    ("open") are identified, with license and download links, through the PMC Article Datasets
    on AWS. Other articles get a DOI link with unknown access.
    """

    def __init__(self, session: requests.Session, timeout: float = 30, workers: int = 8):
        self.session = session
        self.timeout = timeout
        self.workers = workers

    def annotate(self, articles: list[Article]) -> int:
        """Set ``fulltext`` on each article. Returns the number of PMC lookups that failed."""
        pmc_articles = []
        for article in articles:
            article["fulltext"] = None
            if article.get("pmc_id"):
                pmcid = normalize_pmcid(article["pmc_id"])
                article["fulltext"] = self._record(f"https://pmc.ncbi.nlm.nih.gov/articles/{pmcid}/", "PMC", "free")
                pmc_articles.append(article)
            elif accession := (article.get("article_ids") or {}).get("bookaccession"):
                article["fulltext"] = self._record(
                    f"https://www.ncbi.nlm.nih.gov/books/{accession}/", "NCBI Bookshelf", "free"
                )
            elif article.get("doi"):
                article["fulltext"] = self._record(
                    f"https://doi.org/{article['doi']}", "Publisher (via DOI)", "unknown"
                )

        failures = 0
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            pmcids = [normalize_pmcid(a["pmc_id"]) for a in pmc_articles]
            for article, result in zip(pmc_articles, pool.map(self.dataset_metadata, pmcids), strict=True):
                if isinstance(result, Exception):
                    failures += 1
                    log.debug("PMC dataset lookup failed for %s: %s", article["pmc_id"], result)
                    continue
                article["fulltext"]["oa_checked"] = True
                if result:
                    self._apply(article["fulltext"], result)
        if failures:
            log.warning("Could not check open-access status for %d PMC articles.", failures)
        return failures

    @staticmethod
    def _record(url: str, source: str, access_type: str) -> dict[str, Any]:
        return {
            "url": url,
            "source": source,
            "access_type": access_type,
            "oa_checked": source != "PMC",
            "license": None,
            "is_manuscript": None,
            "retracted": None,
            "download_links": [],
        }

    def dataset_metadata(self, pmcid: str) -> dict[str, Any] | None | Exception:
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
            versions = [text(p).rstrip("/") for p in root.findall("s3:CommonPrefixes/s3:Prefix", S3_NS)]
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
    def _apply(fulltext: dict[str, Any], meta: dict[str, Any]) -> None:
        fulltext["access_type"] = "open" if meta.get("is_pmc_openaccess") else "free"
        fulltext["license"] = meta.get("license_code")
        fulltext["is_manuscript"] = bool(meta.get("is_manuscript"))
        fulltext["retracted"] = bool(meta.get("is_retracted"))
        fulltext["download_links"] = [
            {"format": fmt, "url": s3_to_https(meta[key]), "path": None}
            for fmt, key in (("pdf", "pdf_url"), ("xml", "xml_url"), ("txt", "text_url"))
            if meta.get(key)
        ]

    def download(self, articles: Iterable[Article], directory: str | Path, formats=("pdf",)) -> tuple[int, int]:
        """
        Download available files (from ``fulltext.download_links``) into ``directory``. Existing
        files are skipped. Each link gets a ``path`` to the local file. Returns (downloaded, failed).
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
            return 0, 0

        log.info("Downloading %d full-text files to %s...", len(links), directory)
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            results = list(pool.map(lambda link: self._download_file(link, directory), links))
        downloaded, failed = results.count(True), results.count(False)
        if failed:
            log.warning("Failed to download %d of %d files.", failed, len(links))
        log.info("Downloaded %d files (%d already present).", downloaded, results.count(None))
        return downloaded, failed

    def _download_file(self, link: dict[str, Any], directory: Path) -> bool | None:
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
