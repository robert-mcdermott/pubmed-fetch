import gzip
from datetime import date

import pytest
import requests
from conftest import FakeResponse, FakeSession, article_set, efetch_handler, esearch_handler, esearch_xml

from pubmed_fetch import PubMedClient, PubMedError, QuerySpec
from pubmed_fetch.client import PUBMED_RECORD_LIMIT

START, END = date(2024, 1, 1), date(2024, 1, 31)


def make_client(routes, **kwargs):
    return PubMedClient(session=FakeSession(routes), **kwargs)


def collect(batches):
    return [a for batch in batches for a in batch]


def test_esearch_params_and_provenance():
    client = make_client({"esearch.fcgi": esearch_handler(0)}, email="you@example.com", api_key="KEY")
    articles = collect(client.iter_search(QuerySpec(authors=["Smith J", "Lee K"]), START, END, "edat"))

    assert articles == []
    params = client.session.calls_to("esearch.fcgi")[0]
    assert params["term"] == '("Smith J"[Author] OR "Lee K"[Author])'
    assert (params["datetype"], params["mindate"], params["maxdate"]) == ("edat", "2024/01/01", "2024/01/31")
    assert params["usehistory"] == "y"
    assert (params["tool"], params["email"], params["api_key"]) == ("pubmed-fetch", "you@example.com", "KEY")
    info = client.search_info
    assert info["query_translation"] == "translated"
    assert info["filters"]["authors"] == ["Smith J", "Lee K"]
    assert info["date_range"] == {"start_date": "2024-01-01", "end_date": "2024-01-31"}
    assert (info["total_found"], info["retrieved"], info["failed_batches"]) == (0, 0, [])


def test_query_warnings_are_logged_and_strict_mode_fails(caplog):
    warning = '<WarningList><QuotedPhraseNotFound>"x"[MeSH Terms]</QuotedPhraseNotFound></WarningList>'
    error = "<ErrorList><FieldNotFound>BadField</FieldNotFound></ErrorList>"
    routes = {"esearch.fcgi": esearch_handler(0, extra=warning + error)}

    client = make_client(routes)
    collect(client.iter_search(QuerySpec(mesh_terms=["x"]), START, END))
    assert client.search_info["query_warnings"] == ["FieldNotFound: BadField", 'QuotedPhraseNotFound: "x"[MeSH Terms]']
    assert "PubMed query warning: FieldNotFound: BadField" in caplog.text

    with pytest.raises(PubMedError, match="strict mode"):
        collect(make_client(routes, strict=True).iter_search(QuerySpec(mesh_terms=["x"]), START, END))


def test_search_pages_through_history_and_caps_results(caplog):
    client = make_client({"esearch.fcgi": esearch_handler(1000, webenv="ENV"), "efetch.fcgi": efetch_handler()})
    client.batch_size = 100
    articles = collect(client.iter_search(QuerySpec(mesh_terms=["X"]), START, END, max_results=250))

    calls = client.session.calls_to("efetch.fcgi")
    assert [(c["retstart"], c["retmax"]) for c in calls] == [(0, 100), (100, 100), (200, 50)]
    assert all(c["WebEnv"] == "ENV" and c["query_key"] == "1" for c in calls)
    assert len(articles) == 250
    assert client.search_info["truncated"] is True and client.search_info["expected"] == 250
    assert "Only 250 of 1000" in caplog.text


def test_large_searches_are_split_into_date_windows():
    counts = {
        ("2024/01/01", "2024/01/31"): 15000,
        ("2024/01/01", "2024/01/16"): 6000,
        ("2024/01/17", "2024/01/31"): 9000,
    }

    def esearch(url, params):
        key = (params["mindate"], params["maxdate"])
        return FakeResponse(esearch_xml(counts[key], webenv=f"{key[0]}-{key[1]}"))

    client = make_client({"esearch.fcgi": esearch, "efetch.fcgi": efetch_handler()})
    client.batch_size = 5000
    articles = collect(client.iter_search(QuerySpec(mesh_terms=["X"]), START, END, max_results=20000))

    assert len(articles) == 15000
    webenvs = [c["WebEnv"] for c in client.session.calls_to("efetch.fcgi")]
    # Newest window first, each fetched in full.
    assert webenvs == ["2024/01/17-2024/01/31"] * 2 + ["2024/01/01-2024/01/16"] * 2
    assert client.search_info["date_windows"] == 2
    assert client.search_info["truncated"] is False


def test_no_splitting_when_fewer_than_the_limit_are_wanted():
    client = make_client({"esearch.fcgi": esearch_handler(50000), "efetch.fcgi": efetch_handler()})
    collect(client.iter_search(QuerySpec(mesh_terms=["X"]), START, END, max_results=10))
    assert len(client.session.calls_to("esearch.fcgi")) == 1


def test_single_day_over_the_limit_is_capped(caplog):
    client = make_client({"esearch.fcgi": esearch_handler(PUBMED_RECORD_LIMIT + 5), "efetch.fcgi": efetch_handler()})
    client.batch_size = 5000
    day = date(2024, 1, 1)
    articles = collect(client.iter_search(QuerySpec(mesh_terms=["X"]), day, day, max_results=20000))
    assert len(articles) == PUBMED_RECORD_LIMIT
    assert client.search_info["truncated"] is True
    assert "only 10000 can be retrieved" in caplog.text


def test_search_raises_on_network_or_eutils_error():
    def boom(url, params):
        raise requests.ConnectionError("down")

    with pytest.raises(PubMedError, match="Error searching PubMed"):
        collect(make_client({"esearch.fcgi": boom}).iter_search(QuerySpec(mesh_terms=["X"]), START, END))
    bad = make_client({"esearch.fcgi": lambda u, p: FakeResponse(b"<eSearchResult><ERROR>bad</ERROR></eSearchResult>")})
    with pytest.raises(PubMedError, match="bad"):
        collect(bad.iter_search(QuerySpec(mesh_terms=["X"]), START, END))


def test_failed_batches_are_recorded_and_retried(records):
    attempts = {"28298962": 0}

    def efetch(url, params):
        if params["id"] == "10115318":
            return FakeResponse(status=500)
        if params["id"] == "28298962":
            attempts["28298962"] += 1
            if attempts["28298962"] == 1:
                return FakeResponse(status=400)  # transient history-server error, retried
        return FakeResponse(article_set(records[params["id"]]))

    client = make_client({"efetch.fcgi": efetch})
    client.batch_size = 1
    articles = collect(client.iter_pmids(["28298962", "10115318", "28298962"]))

    assert [a["pmid"] for a in articles] == ["28298962"]  # duplicates removed, failed batch skipped
    failed = client.search_info["failed_batches"]
    assert len(failed) == 1 and failed[0]["pmids"] == ["10115318", "10115318"] and "500" in failed[0]["error"]
    assert attempts["28298962"] == 2


def test_save_raw_and_reparse(tmp_path, records):
    raw = tmp_path / "raw"
    client = make_client({"efetch.fcgi": efetch_handler(records)}, raw_dir=raw)
    fetched = client.fetch_article_details(["28298962", "33237689"])
    files = sorted(raw.iterdir())
    assert [f.name for f in files] == ["efetch-00001.xml.gz"]
    assert b"28298962" in gzip.decompress(files[0].read_bytes())

    (raw / "notes.txt").write_text("ignored")
    offline = PubMedClient(session=FakeSession({}))
    reparsed = collect(offline.iter_raw(raw))
    assert reparsed == fetched
    assert offline.search_info["source"] == "raw"

    with pytest.raises(PubMedError, match="No .xml"):
        collect(offline.iter_raw(tmp_path))  # contains only the raw/ subdirectory


def test_search_pubmed_convenience_wrapper():
    client = make_client({"esearch.fcgi": esearch_handler(3), "efetch.fcgi": efetch_handler()})
    articles = client.search_pubmed(mesh_terms=["X"], start_date=START, end_date=END, journals=["Nature"])
    assert len(articles) == 3
    assert '("Nature"[Journal])' in client.session.calls_to("esearch.fcgi")[0]["term"]
