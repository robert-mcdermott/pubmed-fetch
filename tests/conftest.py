import os
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
import requests

import pubmed_fetch.client

FIXTURES = Path(__file__).parent / "fixtures"
SAMPLE_XML = FIXTURES / "pubmed_efetch_sample.xml"


def pytest_collection_modifyitems(config, items):
    if os.environ.get("PUBMED_FETCH_LIVE") == "1":
        return
    skip = pytest.mark.skip(reason="live API test; set PUBMED_FETCH_LIVE=1 to run")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch, request):
    if "live" not in request.keywords:
        monkeypatch.setattr(pubmed_fetch.client.time, "sleep", lambda seconds: None)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, request):
    if "live" in request.keywords:
        return
    monkeypatch.delenv("NCBI_EMAIL", raising=False)
    monkeypatch.delenv("NCBI_API_KEY", raising=False)


class FakeResponse:
    def __init__(self, content=b"", status=200, json_data=None):
        self.content = content
        self.status_code = status
        self._json = json_data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} error")

    def json(self):
        return self._json

    def iter_content(self, chunk_size=1):
        yield self.content

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeSession:
    """Routes requests to handlers keyed by URL substring (first match wins) and records calls."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def _handle(self, method, url, params):
        self.calls.append((method, url, params))
        for key, handler in self.routes.items():
            if key in url:
                return handler(url, params)
        raise AssertionError(f"Unexpected request: {url}")

    def get(self, url, params=None, **kwargs):
        return self._handle("GET", url, params or {})

    def post(self, url, data=None, **kwargs):
        return self._handle("POST", url, data or {})

    def calls_to(self, fragment):
        return [params for _, url, params in self.calls if fragment in url]


def esearch_xml(count, webenv="WE", query_key="1", extra=""):
    return (
        f"<eSearchResult><Count>{count}</Count><QueryKey>{query_key}</QueryKey><WebEnv>{webenv}</WebEnv>"
        f"<IdList/><QueryTranslation>translated</QueryTranslation>{extra}</eSearchResult>"
    ).encode()


def esearch_handler(count, **kwargs):
    return lambda url, params: FakeResponse(esearch_xml(count, **kwargs))


def sample_records():
    """The real PubMed records in the fixture, keyed by PMID."""
    root = ET.parse(SAMPLE_XML).getroot()
    return {r.findtext(".//PMID"): r for r in root}


def article_set(*records):
    return b"<PubmedArticleSet>" + b"".join(ET.tostring(r) for r in records) + b"</PubmedArticleSet>"


MINIMAL_ARTICLE = (
    "<PubmedArticle><MedlineCitation><PMID>{pmid}</PMID><Article><ArticleTitle>Evaluation of response {pmid}"
    "</ArticleTitle></Article></MedlineCitation><PubmedData/></PubmedArticle>"
)


def efetch_handler(records=None):
    """
    Serve efetch requests: by ``id`` from the fixture records, or for history-server batches
    (retstart/retmax) as minimal records with PMIDs 90000000 + position.
    """
    records = records or sample_records()

    def handler(url, params):
        if "id" in params:
            chosen = [records[p] for p in params["id"].split(",") if p in records]
            return FakeResponse(article_set(*chosen))
        start, n = int(params["retstart"]), int(params["retmax"])
        body = "".join(MINIMAL_ARTICLE.format(pmid=90000000 + start + i) for i in range(n))
        return FakeResponse(f"<PubmedArticleSet>{body}</PubmedArticleSet>".encode())

    return handler


@pytest.fixture
def records():
    return sample_records()
