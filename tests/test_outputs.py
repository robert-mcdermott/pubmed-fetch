import csv
import json
import sqlite3
import xml.etree.ElementTree as ET

import pytest
from conftest import SAMPLE_XML

from pubmed_fetch import open_sink, parse_records, resolve_output, save_results
from pubmed_fetch.outputs import CSV_FIELDS, csv_row

INFO = {"query": "q", "complete": True}


@pytest.fixture
def articles():
    parsed, _ = parse_records(ET.parse(SAMPLE_XML).getroot())
    parsed[0]["fulltext"] = {"url": "https://pmc/1", "access_type": "open", "license": "CC BY"}
    parsed[0]["metrics"] = {"source": "icite", "citation_count": 15, "relative_citation_ratio": 0.43}
    return parsed


@pytest.mark.parametrize(
    ("filename", "fmt", "expected"),
    [
        (None, None, ("pubmed_articles.json", "json")),
        (None, "csv", ("pubmed_articles.csv", "csv")),
        (None, "sqlite", ("pubmed_articles.sqlite", "sqlite")),
        ("out.JSONL", None, ("out.JSONL", "jsonl")),
        ("out.ndjson", None, ("out.ndjson", "jsonl")),
        ("out.db", None, ("out.db", "sqlite")),
        ("out.txt", None, ("out.txt", "json")),
        ("out.json", "csv", ("out.json", "csv")),
        ("-", None, ("-", "json")),
    ],
)
def test_resolve_output(filename, fmt, expected):
    assert resolve_output(filename, fmt) == expected


def test_json_output(tmp_path, articles):
    out = tmp_path / "out.json"
    assert save_results(articles, INFO, filename=str(out)) == 9
    doc = json.loads(out.read_text())
    assert doc["schema_version"] == 2
    assert doc["search_info"] == {**INFO, "total_articles": 9}
    assert [a["pmid"] for a in doc["articles"]] == [a["pmid"] for a in articles]
    assert not list(tmp_path.glob("*.tmp"))


def test_json_merge_replaces_by_pmid(tmp_path, articles):
    out = str(tmp_path / "out.json")
    save_results(articles[:3], INFO, filename=out)
    updated = {**articles[1], "title": "Updated"}
    assert save_results([updated, articles[3]], INFO, filename=out, merge=True) == 4
    doc = json.loads(open(out).read())
    assert [a["title"] for a in doc["articles"]][1] == "Updated"


def test_jsonl_resume_skips_existing_and_repairs_partial_line(tmp_path, articles, caplog):
    out = tmp_path / "out.jsonl"
    sink = open_sink(str(out), "jsonl")
    sink.write(articles[:2])
    sink.close(INFO)
    with open(out, "a") as f:
        f.write('{"pmid": "trunc')  # interrupted mid-write

    sink = open_sink(str(out), "jsonl", resume=True)
    assert sink.existing_pmids == {articles[0]["pmid"], articles[1]["pmid"]}
    sink.write([articles[2]])
    assert sink.close(INFO) == 3
    lines = out.read_text().splitlines()
    assert [json.loads(line)["pmid"] for line in lines] == [a["pmid"] for a in articles[:3]]
    assert "incomplete last line" in caplog.text


def test_jsonl_merge(tmp_path, articles):
    out = str(tmp_path / "out.jsonl")
    save_results(articles[:3], INFO, filename=out)
    assert save_results([{**articles[0], "title": "New"}], INFO, filename=out, merge=True) == 3
    records = [json.loads(line) for line in open(out)]
    assert [r["pmid"] for r in records] == [articles[0]["pmid"], articles[1]["pmid"], articles[2]["pmid"]]
    assert records[0]["title"] == "New"


def test_csv_output_and_merge(tmp_path, articles):
    out = tmp_path / "out.csv"
    save_results(articles, INFO, filename=str(out))
    rows = list(csv.DictReader(out.open(encoding="utf-8", newline="")))
    assert list(rows[0]) == CSV_FIELDS
    first = rows[0]
    assert first["authors"].startswith("Bauckneht, Matteo; Piva, Roberta")
    assert (first["first_author"], first["last_author"]) == ("Bauckneht, Matteo", "Morbelli, Silvia")
    assert (first["access_type"], first["license"], first["citation_count"]) == ("open", "CC BY", "15")
    grants_row = next(r for r in rows if r["pmid"] == "35187501")
    assert grants_row["grant_agencies"] == "NCI NIH HHS; NIBIB NIH HHS; NIA NIH HHS"  # de-duplicated, in order
    assert "0000-0002-8440-2474" in grants_row["orcids"]
    major = next(r for r in rows if r["pmid"] == "10029841")["mesh_major_topics"].split("; ")
    assert "Disorders of Sex Development" in major  # major via its "surgery" qualifier
    assert "Child" not in major

    save_results([{**articles[0], "title": "Changed"}], INFO, filename=str(out), merge=True)
    rows = list(csv.DictReader(out.open(encoding="utf-8", newline="")))
    assert len(rows) == 9 and rows[0]["title"] == "Changed"


def test_csv_row_has_no_none_values(articles):
    assert None not in csv_row(articles[6]).values()


def test_sqlite_output_is_normalized_and_upserts(tmp_path, articles):
    out = str(tmp_path / "out.sqlite")
    assert save_results(articles, INFO, filename=out) == 9
    assert save_results([{**articles[0], "title": "Again"}], INFO, filename=out) == 9

    db = sqlite3.connect(out)
    assert db.execute("SELECT title, citation_count FROM articles WHERE pmid='28298962'").fetchone() == ("Again", 15)
    assert db.execute("SELECT COUNT(*) FROM authors WHERE pmid='28298962'").fetchone() == (5,)
    assert db.execute("SELECT orcid FROM authors WHERE pmid='35187501' AND position=1").fetchone() == (
        "0000-0002-8440-2474",
    )
    assert db.execute("SELECT COUNT(*) FROM affiliations WHERE pmid='35187501' AND author_position=1").fetchone() == (
        3,
    )
    mesh_query = "SELECT qualifier, qualifier_major FROM mesh WHERE pmid='10029841' AND descriptor=?"
    assert db.execute(mesh_query, ("Disorders of Sex Development",)).fetchall() == [
        ("diagnosis", 0),
        ("psychology", 0),
        ("surgery", 1),
    ]
    assert db.execute("SELECT agency FROM grants WHERE pmid='35187501' LIMIT 1").fetchone() == ("NCI NIH HHS",)
    assert db.execute("SELECT is_retracted FROM articles WHERE pmid='36452530'").fetchone() == (1,)
    assert db.execute("SELECT COUNT(*) FROM runs").fetchone() == (2,)
    record = json.loads(db.execute("SELECT record_json FROM articles WHERE pmid='33237689'").fetchone()[0])
    assert record["record_type"] == "book"

    sink = open_sink(out, "sqlite", resume=True)
    assert len(sink.existing_pmids) == 9
    sink.close(INFO)


def test_stdout_output(capsys, articles):
    save_results(articles[:2], INFO, filename="-", fmt="jsonl")
    lines = capsys.readouterr().out.splitlines()
    assert [json.loads(line)["pmid"] for line in lines] == [articles[0]["pmid"], articles[1]["pmid"]]


def test_invalid_sink_combinations():
    with pytest.raises(ValueError):
        open_sink("-", "sqlite")
    with pytest.raises(ValueError):
        open_sink("out.json", "json", merge=True, resume=True)
