import json
from datetime import date, timedelta

import pytest
from conftest import FakeResponse, FakeSession, efetch_handler, esearch_handler

from pubmed_fetch import PUBMED_RECORD_LIMIT, Options, PubMedClient, PubMedError, QuerySpec, run
from pubmed_fetch.pipeline import EXIT_OK, EXIT_PARTIAL


def options(tmp_path, **kwargs):
    defaults = {"spec": QuerySpec(mesh_terms=["X"]), "fulltext": False, "display": False}
    return Options(**{**defaults, "output_file": str(tmp_path / "out.json"), **kwargs})


def test_run_writes_complete_output(tmp_path):
    session = FakeSession({"esearch.fcgi": esearch_handler(3), "efetch.fcgi": efetch_handler()})
    result = run(options(tmp_path), session=session)

    assert result.exit_code == EXIT_OK
    doc = json.loads((tmp_path / "out.json").read_text())
    info = doc["search_info"]
    assert info["tool"]["name"] == "pubmed-fetch"
    assert info["generated_on"].endswith("Z")
    assert (info["complete"], info["total_articles"], info["retrieved"], info["date_type"]) == (True, 3, 3, "pdat")


def flaky_efetch(url, params):
    """Fail the first batch of every search."""
    return FakeResponse(status=500) if params["retstart"] == 0 else efetch_handler()(url, params)


def test_run_reports_partial_results(tmp_path, monkeypatch):
    monkeypatch.setattr(PubMedClient, "batch_size", 2)
    session = FakeSession({"esearch.fcgi": esearch_handler(4), "efetch.fcgi": flaky_efetch})
    result = run(options(tmp_path), session=session)

    assert result.exit_code == EXIT_PARTIAL
    info = result.search_info
    assert info["complete"] is False
    assert info["failed_batches"][0]["retstart"] == 0
    assert info["total_articles"] == 2


def test_run_fails_when_nothing_could_be_fetched(tmp_path):
    session = FakeSession({"esearch.fcgi": esearch_handler(2), "efetch.fcgi": lambda u, p: FakeResponse(status=400)})
    with pytest.raises(PubMedError, match="Could not fetch any records"):
        run(options(tmp_path), session=session)
    assert not (tmp_path / "out.json").exists()


def test_run_writes_empty_output_for_no_matches(tmp_path):
    run(options(tmp_path), session=FakeSession({"esearch.fcgi": esearch_handler(0)}))
    doc = json.loads((tmp_path / "out.json").read_text())
    assert doc["articles"] == [] and doc["search_info"]["total_articles"] == 0


def test_since_last_run_uses_and_updates_state(tmp_path):
    state = tmp_path / "state.json"
    session = FakeSession({"esearch.fcgi": esearch_handler(1), "efetch.fcgi": efetch_handler()})
    opts = options(tmp_path, since_last_run=str(state), days=3, output_file=str(tmp_path / "out.jsonl"))

    run(opts, session=session)
    first = session.calls_to("esearch.fcgi")[0]
    today = date.today()
    assert first["datetype"] == "mdat"
    assert first["mindate"] == (today - timedelta(days=3)).strftime("%Y/%m/%d")
    saved = json.loads(state.read_text())
    assert (saved["last_end_date"], saved["date_type"]) == (today.isoformat(), "mdat")

    state.write_text(json.dumps({**saved, "last_end_date": "2024-05-01"}))
    run(
        options(tmp_path, since_last_run=str(state), output_file=str(tmp_path / "out.jsonl"), merge=True),
        session=session,
    )
    assert session.calls_to("esearch.fcgi")[1]["mindate"] == "2024/05/01"


def test_since_last_run_state_not_updated_on_partial_run(tmp_path):
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"last_end_date": "2024-05-01", "query": None, "date_type": "edat"}))
    session = FakeSession({"esearch.fcgi": esearch_handler(400), "efetch.fcgi": flaky_efetch})
    result = run(options(tmp_path, since_last_run=str(state)), session=session)
    assert result.exit_code == EXIT_PARTIAL
    assert json.loads(state.read_text())["last_end_date"] == "2024-05-01"


def test_since_last_run_state_not_updated_on_truncated_run(tmp_path, caplog):
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"last_end_date": "2024-05-01", "query": None, "date_type": "edat"}))
    session = FakeSession({"esearch.fcgi": esearch_handler(5), "efetch.fcgi": efetch_handler()})
    result = run(options(tmp_path, since_last_run=str(state), max_results=2), session=session)
    # The run itself succeeded, but advancing would skip the 3 articles that weren't retrieved.
    assert result.exit_code == EXIT_OK and result.search_info["truncated"]
    assert json.loads(state.read_text())["last_end_date"] == "2024-05-01"
    assert "--max-results cut the results" in caplog.text


def test_since_last_run_advances_past_pubmeds_daily_limit(tmp_path, monkeypatch):
    # More than 10,000 matches on one day can never be retrieved, so holding the state back would
    # stall every later run on the same window.
    monkeypatch.setattr(PubMedClient, "batch_size", 5000)
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"last_end_date": date.today().isoformat(), "query": None, "date_type": "mdat"}))
    session = FakeSession({"esearch.fcgi": esearch_handler(PUBMED_RECORD_LIMIT + 5), "efetch.fcgi": efetch_handler()})
    opts = options(tmp_path, since_last_run=str(state), max_results=None, output_file=str(tmp_path / "out.jsonl"))
    result = run(opts, session=session)
    assert result.search_info["truncated"] and result.search_info["max_results"] is None
    # save_state wrote it (the initial state had no last_total_found).
    assert json.loads(state.read_text())["last_total_found"] == PUBMED_RECORD_LIMIT + 5


def test_state_written_with_edat_explains_the_new_default(tmp_path, caplog):
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"last_end_date": "2024-05-01", "query": '("X"[MeSH Terms])', "date_type": "edat"}))
    session = FakeSession({"esearch.fcgi": esearch_handler(1), "efetch.fcgi": efetch_handler()})
    run(options(tmp_path, since_last_run=str(state)), session=session)
    assert "Pass --date-type edat to keep the previous behavior" in caplog.text
    assert "different query" not in caplog.text


def test_resume_skips_existing_records(tmp_path):
    out = tmp_path / "out.jsonl"
    session = FakeSession({"esearch.fcgi": esearch_handler(3), "efetch.fcgi": efetch_handler()})
    run(options(tmp_path, output_file=str(out)), session=session)
    result = run(options(tmp_path, output_file=str(out), resume=True), session=session)
    assert result.search_info["skipped_existing"] == 3
    assert result.search_info["total_articles"] == 3
    assert len(out.read_text().splitlines()) == 3


def test_pmids_mode_with_enrichment(tmp_path):
    def icite(url, params):
        return FakeResponse(json_data={"data": [{"pmid": 28298962, "citation_count": 15}]})

    s3_empty = '<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/"></ListBucketResult>'
    session = FakeSession(
        {
            "efetch.fcgi": efetch_handler(),
            "icite.od.nih.gov": icite,
            "amazonaws.com": lambda u, p: FakeResponse(s3_empty.encode()),
        }
    )
    opts = options(tmp_path, spec=QuerySpec(), pmids=["28298962", "33237689"], fulltext=True, icite=True)
    info = run(opts, session=session).search_info
    assert info["source"] == "pmids"
    articles = {a["pmid"]: a for a in json.loads((tmp_path / "out.json").read_text())["articles"]}
    assert articles["28298962"]["metrics"]["citation_count"] == 15
    assert articles["28298962"]["fulltext"]["access_type"] == "free"
    assert articles["33237689"]["fulltext"]["source"] == "NCBI Bookshelf"


def test_listing_is_printed_unless_output_is_stdout(tmp_path, capsys):
    session = FakeSession({"esearch.fcgi": esearch_handler(1), "efetch.fcgi": efetch_handler()})
    run(options(tmp_path, display=True), session=session)
    assert "1. Evaluation of response" in capsys.readouterr().out

    run(options(tmp_path, display=True, output_file="-", format="jsonl"), session=session)
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 1 and json.loads(lines[0])["pmid"] == "90000000"
