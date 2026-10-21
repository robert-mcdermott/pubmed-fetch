"""Citation metrics from NIH iCite (https://icite.od.nih.gov/api)."""

from __future__ import annotations

import logging

import requests

from .parse import Article

log = logging.getLogger(__name__)

ICITE_URL = "https://icite.od.nih.gov/api/pubs"
ICITE_FIELDS = (
    "citation_count",
    "relative_citation_ratio",
    "nih_percentile",
    "field_citation_rate",
    "expected_citations_per_year",
    "citations_per_year",
    "apt",
    "is_clinical",
    "is_research_article",
)


def add_icite_metrics(
    session: requests.Session, articles: list[Article], timeout: float = 30, batch_size: int = 200
) -> int:
    """
    Set ``metrics`` on each article from iCite (None if iCite has no record yet, which is
    normal for very recent articles). Returns the number of articles whose lookup failed.
    """
    failures = 0
    for i in range(0, len(articles), batch_size):
        batch = articles[i : i + batch_size]
        try:
            response = session.get(
                ICITE_URL,
                params={
                    "pmids": ",".join(a["pmid"] for a in batch),
                    "format": "json",
                    "fl": ",".join(("pmid", *ICITE_FIELDS)),
                },
                timeout=timeout,
            )
            response.raise_for_status()
            records = {str(r["pmid"]): r for r in response.json().get("data", [])}
        except (requests.RequestException, ValueError, KeyError) as e:
            failures += len(batch)
            log.debug("iCite lookup failed: %s", e)
            continue
        for article in batch:
            record = records.get(article["pmid"])
            article["metrics"] = {"source": "icite", **{f: record.get(f) for f in ICITE_FIELDS}} if record else None
    if failures:
        log.warning("Could not fetch iCite metrics for %d articles.", failures)
    return failures
