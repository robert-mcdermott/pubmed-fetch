"""The output must match the published JSON Schema, so changes to it are deliberate."""

import json
import xml.etree.ElementTree as ET

import jsonschema
import pytest
from conftest import SAMPLE_XML, FakeResponse, FakeSession, efetch_handler, esearch_handler

from pubmed_fetch import Options, PMCFullText, QuerySpec, parse_records, run
from pubmed_fetch.schema import article_schema, load_schema


@pytest.fixture(scope="module")
def validator():
    jsonschema.Draft202012Validator.check_schema(load_schema())
    return jsonschema.Draft202012Validator(article_schema())


def test_every_fixture_record_matches_schema(validator):
    articles, _ = parse_records(ET.parse(SAMPLE_XML).getroot())
    for article in articles:
        validator.validate(article)


def test_enriched_record_matches_schema(validator):
    articles, _ = parse_records(ET.parse(SAMPLE_XML).getroot())
    s3_empty = b'<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/"></ListBucketResult>'
    PMCFullText(FakeSession({"amazonaws.com": lambda u, p: FakeResponse(s3_empty)})).annotate(articles)
    articles[0]["metrics"] = {"source": "icite", "citation_count": 1, "relative_citation_ratio": 0.5}
    for article in articles:
        validator.validate(article)


def test_full_output_document_matches_schema(tmp_path):
    session = FakeSession({"esearch.fcgi": esearch_handler(2), "efetch.fcgi": efetch_handler()})
    out = tmp_path / "out.json"
    run(Options(spec=QuerySpec(mesh_terms=["X"]), fulltext=False, display=False, output_file=str(out)), session=session)
    jsonschema.validate(json.loads(out.read_text()), load_schema())


def test_schema_rejects_unexpected_fields(validator):
    articles, _ = parse_records(ET.parse(SAMPLE_XML).getroot())
    with pytest.raises(jsonschema.ValidationError):
        validator.validate({**articles[0], "surprise": 1})
