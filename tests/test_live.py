"""
Contract tests against the real NCBI, PMC and iCite APIs, to catch upstream changes (such as
the PMC OA Web Service retirement in 2026). Skipped unless PUBMED_FETCH_LIVE=1; CI runs them weekly.
"""

import os
from datetime import date

import jsonschema
import pytest

from pubmed_fetch import PMCFullText, PubMedClient, QuerySpec, add_icite_metrics
from pubmed_fetch.schema import article_schema

pytestmark = pytest.mark.live

KNOWN_PMID = "28298962"  # PMC5334499, open access (CC BY-NC), cited in iCite


@pytest.fixture(scope="module")
def client():
    return PubMedClient(email=os.environ.get("NCBI_EMAIL"), api_key=os.environ.get("NCBI_API_KEY") or None)


def test_search_and_fetch(client):
    batches = client.iter_search(
        QuerySpec(mesh_terms=["Breast Neoplasms"]), date(2024, 1, 1), date(2024, 1, 7), "pdat", 5
    )
    articles = [a for batch in batches for a in batch]
    assert len(articles) == 5
    assert client.search_info["total_found"] > 5
    assert "Breast Neoplasms" in (client.search_info["query_translation"] or "")
    validator = jsonschema.Draft202012Validator(article_schema())
    for article in articles:
        validator.validate(article)


def test_query_warnings_are_reported(client):
    result = client.esearch('"Breast Neoplasm xyzzy"[MeSH Terms]', date(2024, 1, 1), date(2024, 1, 7))
    assert result.count == 0
    assert any("QuotedPhraseNotFound" in w for w in result.warnings)


def test_known_article_fulltext_and_metrics(client):
    [article] = client.fetch_article_details([KNOWN_PMID])
    assert article["pmc_id"] == "PMC5334499"
    PMCFullText(client.session).annotate([article])
    fulltext = article["fulltext"]
    assert (fulltext["access_type"], fulltext["oa_checked"]) == ("open", True)
    assert any(link["format"] == "pdf" for link in fulltext["download_links"])
    add_icite_metrics(client.session, [article])
    assert article["metrics"]["citation_count"] >= 15
