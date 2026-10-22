"""NCBI E-utilities client: search PubMed and fetch records in batches."""

from __future__ import annotations

import gzip
import logging
import time
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ._meta import TOOL_NAME, PubMedError
from .parse import Article, opt, parse_records, text
from .query import DATE_TYPES, QuerySpec

log = logging.getLogger(__name__)

EUTILS_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"
# PubMed's E-utilities will not return records beyond the first 10,000 of a search.
PUBMED_RECORD_LIMIT = 10_000


def make_session(pool_size: int = 10) -> requests.Session:
    """A requests session that retries transient failures (429/5xx) with backoff."""
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


@dataclass
class SearchResult:
    """One esearch call, with its result set stored on the history server."""

    count: int
    webenv: str
    query_key: str
    start: date | None = None
    end: date | None = None
    query_translation: str | None = None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class PubMedClient:
    """
    Search PubMed and fetch records. Batches are yielded as lists of article dicts; details of
    the run (query, counts, failed batches) are collected in ``self.search_info``.
    """

    batch_size = 200
    efetch_attempts = 2

    def __init__(
        self,
        email: str | None = None,
        api_key: str | None = None,
        tool: str = TOOL_NAME,
        session: requests.Session | None = None,
        timeout: float = 30,
        strict: bool = False,
        raw_dir: str | Path | None = None,
    ):
        self.base_url = EUTILS_URL
        self.tool = tool
        self.email = email
        self.api_key = api_key
        self.timeout = timeout
        self.strict = strict
        self.raw_dir = Path(raw_dir) if raw_dir else None
        self.session = session or make_session()
        self.search_info: dict[str, Any] = {}
        self._last_request = 0.0
        self._raw_count = 0

    @property
    def min_interval(self) -> float:
        # NCBI allows 3 requests/second without an API key and 10/second with one.
        return 0.1 if self.api_key else 0.34

    def _request(self, endpoint: str, params: dict[str, Any]) -> bytes:
        """Make a rate-limited E-utilities request and return the response body."""
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
        return response.content

    def _eutils(self, endpoint: str, params: dict[str, Any]) -> ET.Element:
        root = ET.fromstring(self._request(endpoint, params))
        error = root.find("ERROR")
        if error is not None:
            raise PubMedError(text(error))
        return root

    # -- searching -------------------------------------------------------------------------

    def esearch(self, term: str, start: date, end: date, date_type: str = "pdat") -> SearchResult:
        """Run a search over [start, end] and keep the results on the history server."""
        if date_type not in DATE_TYPES:
            raise PubMedError(f"Unknown date type {date_type!r}; use one of {', '.join(DATE_TYPES)}.")
        try:
            root = self._eutils(
                "esearch.fcgi",
                {
                    "db": "pubmed",
                    "term": term,
                    "datetype": date_type,
                    "mindate": start.strftime("%Y/%m/%d"),
                    "maxdate": end.strftime("%Y/%m/%d"),
                    "usehistory": "y",
                    "retmax": 0,
                },
            )
        except (requests.RequestException, ET.ParseError) as e:
            raise PubMedError(f"Error searching PubMed: {e}") from e
        return SearchResult(
            count=int(text(root.find("Count")) or 0),
            webenv=text(root.find("WebEnv")),
            query_key=text(root.find("QueryKey")),
            start=start,
            end=end,
            query_translation=opt(root.find("QueryTranslation")),
            errors=[f"{e.tag}: {text(e)}" for e in root.findall("ErrorList/*")],
            warnings=[f"{e.tag}: {text(e)}" for e in root.findall("WarningList/*") if e.tag != "OutputMessage"],
        )

    def _check_messages(self, result: SearchResult) -> None:
        """Surface PubMed's query errors/warnings (e.g. a misspelled MeSH term or unknown field tag)."""
        messages = result.errors + result.warnings
        if not messages:
            return
        if self.strict:
            raise PubMedError("PubMed reported problems with the query (strict mode): " + "; ".join(messages))
        for message in messages:
            log.warning("PubMed query warning: %s", message)
        log.warning("PubMed interpreted the query as: %s", result.query_translation)

    def plan_windows(
        self,
        term: str,
        start: date,
        end: date,
        date_type: str = "pdat",
        max_results: int | None = PUBMED_RECORD_LIMIT,
    ) -> list[SearchResult]:
        """
        Search [start, end]. If more than PubMed's 10,000-record limit match and more than that
        are wanted (``max_results=None`` means all), split the date range in half recursively so
        every window can be retrieved in full. The first element is the search over the whole
        range; the rest are the windows (newest first).
        """
        first = self.esearch(term, start, end, date_type)
        self._check_messages(first)
        if first.count <= PUBMED_RECORD_LIMIT or (max_results is not None and max_results <= PUBMED_RECORD_LIMIT):
            return [first, first]
        log.info(
            "%d matches exceed PubMed's %d-record limit; splitting the date range.", first.count, PUBMED_RECORD_LIMIT
        )
        return [first, *reversed(self._split(term, date_type, first))]

    def _split(self, term: str, date_type: str, result: SearchResult) -> list[SearchResult]:
        assert result.start is not None and result.end is not None
        if result.count <= PUBMED_RECORD_LIMIT or result.start == result.end:
            if result.count > PUBMED_RECORD_LIMIT:
                log.warning(
                    "%d articles on %s exceed PubMed's %d-record limit; only %d can be retrieved for that day.",
                    result.count,
                    result.start,
                    PUBMED_RECORD_LIMIT,
                    PUBMED_RECORD_LIMIT,
                )
            return [result]
        mid = result.start + (result.end - result.start) // 2
        halves = [
            self.esearch(term, result.start, mid, date_type),
            self.esearch(term, mid + timedelta(days=1), result.end, date_type),
        ]
        return [w for half in halves for w in self._split(term, date_type, half)]

    def iter_search(
        self,
        spec: QuerySpec,
        start: date,
        end: date,
        date_type: str = "pdat",
        max_results: int | None = 1000,
    ) -> Iterator[list[Article]]:
        """
        Search PubMed and yield batches of parsed articles; ``max_results=None`` fetches every match.
        Raises PubMedError if the search fails.
        """
        term = spec.build()
        if not term:
            raise PubMedError("No search terms provided.")
        log.info("Search query: %s", term)
        log.info("Date range (%s): %s to %s", date_type, start.isoformat(), end.isoformat())

        first, *windows = self.plan_windows(term, start, end, date_type, max_results)
        retrievable = sum(min(w.count, PUBMED_RECORD_LIMIT) for w in windows)
        to_fetch = min(first.count, first.count if max_results is None else max_results, retrievable)
        self._init_info(
            source="search",
            query=term,
            query_translation=first.query_translation,
            query_warnings=first.errors + first.warnings,
            filters=spec.filters(),
            date_type=date_type,
            date_range={"start_date": start.isoformat(), "end_date": end.isoformat()},
            date_windows=len(windows),
            total_found=first.count,
            max_results=max_results,
            expected=to_fetch,
            truncated=to_fetch < first.count,
        )
        log.info("Found %d articles", first.count)
        if to_fetch < first.count:
            log.warning(
                "Only %d of %d matching articles will be retrieved (raise --max-results, use --max-results all, "
                "or narrow the search).",
                to_fetch,
                first.count,
            )

        remaining = to_fetch
        batches: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for window in windows:
            n = min(window.count, PUBMED_RECORD_LIMIT, remaining)
            remaining -= n
            batches.extend(
                (
                    {
                        "WebEnv": window.webenv,
                        "query_key": window.query_key,
                        "retstart": s,
                        "retmax": min(self.batch_size, n - s),
                    },
                    {"window": [str(window.start), str(window.end)], "retstart": s},
                )
                for s in range(0, n, self.batch_size)
            )
        yield from self._iter_efetch(batches)

    def iter_pmids(self, pmids: Iterable[str]) -> Iterator[list[Article]]:
        """Fetch specific PMIDs in batches."""
        pmids = list(dict.fromkeys(p.strip() for p in pmids if p.strip()))
        self._init_info(source="pmids", total_found=len(pmids), expected=len(pmids), truncated=False)
        log.info("Fetching %d PMIDs", len(pmids))
        chunks = [pmids[i : i + self.batch_size] for i in range(0, len(pmids), self.batch_size)]
        yield from self._iter_efetch(
            [({"id": ",".join(chunk)}, {"pmids": [chunk[0], chunk[-1]], "count": len(chunk)}) for chunk in chunks]
        )

    def iter_raw(self, directory: str | Path) -> Iterator[list[Article]]:
        """Re-parse efetch XML saved earlier with ``raw_dir`` (*.xml or *.xml.gz) without contacting PubMed."""
        files = sorted(p for p in Path(directory).iterdir() if p.name.endswith((".xml", ".xml.gz")))
        if not files:
            raise PubMedError(f"No .xml or .xml.gz files found in {directory}")
        self._init_info(source="raw", raw_dir=str(directory), total_found=None, expected=None, truncated=False)
        for path in files:
            try:
                content = gzip.decompress(path.read_bytes()) if path.suffix == ".gz" else path.read_bytes()
                articles, skipped = parse_records(ET.fromstring(content))
            except (OSError, ET.ParseError) as e:
                self._record_failure({"file": path.name}, e)
                continue
            self.search_info["skipped_records"] += skipped
            self.search_info["retrieved"] += len(articles)
            yield articles

    def search_pubmed(
        self,
        mesh_terms=None,
        authors=None,
        organizations=None,
        days_back: int = 30,
        max_results: int | None = 1000,
        start_date: date | None = None,
        end_date: date | None = None,
        date_type: str = "pdat",
        **filters,
    ) -> list[Article]:
        """Convenience wrapper: run a search and return all articles as a list."""
        from .query import resolve_date_range

        spec = QuerySpec(
            mesh_terms=list(mesh_terms or []),
            authors=list(authors or []),
            organizations=list(organizations or []),
            **filters,
        )
        start, end = resolve_date_range(days_back, start_date, end_date)
        return [a for batch in self.iter_search(spec, start, end, date_type, max_results) for a in batch]

    def fetch_article_details(self, pmids: Iterable[str]) -> list[Article]:
        """Convenience wrapper: fetch PMIDs and return all articles as a list."""
        return [a for batch in self.iter_pmids(pmids) for a in batch]

    # -- fetching --------------------------------------------------------------------------

    def _init_info(self, **info: Any) -> None:
        self.search_info = {**info, "retrieved": 0, "skipped_records": 0, "failed_batches": []}

    def _record_failure(self, where: dict[str, Any], error: Exception) -> None:
        self.search_info["failed_batches"].append({**where, "error": str(error)})
        log.warning("Batch %s failed and was skipped: %s", where, error)

    def _save_raw(self, content: bytes) -> None:
        if not self.raw_dir:
            return
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self._raw_count += 1
        (self.raw_dir / f"efetch-{self._raw_count:05d}.xml.gz").write_bytes(gzip.compress(content))

    def _iter_efetch(self, batches: list[tuple[dict[str, Any], dict[str, Any]]]) -> Iterator[list[Article]]:
        for num, (params, where) in enumerate(batches, 1):
            log.info("Fetching batch %d/%d...", num, len(batches))
            error: Exception | None = None
            # The history server intermittently answers 400 for a valid WebEnv, so retry.
            for attempt in range(1, self.efetch_attempts + 1):
                try:
                    content = self._request("efetch.fcgi", {"db": "pubmed", "retmode": "xml", **params})
                    root = ET.fromstring(content)
                    error = None
                    break
                except (requests.RequestException, ET.ParseError) as e:
                    error = e
                    if attempt < self.efetch_attempts:
                        time.sleep(2)
            if error is not None:
                self._record_failure(where, error)
                continue
            self._save_raw(content)
            articles, skipped = parse_records(root)
            self.search_info["skipped_records"] += skipped
            self.search_info["retrieved"] += len(articles)
            yield articles
        log.info("Total articles retrieved: %d", self.search_info["retrieved"])
