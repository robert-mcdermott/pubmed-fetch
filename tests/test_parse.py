"""Parsing tests against real PubMed records saved in tests/fixtures (see conftest.sample_records)."""

import xml.etree.ElementTree as ET

import pytest
from conftest import SAMPLE_XML

from pubmed_fetch import parse_article, parse_records
from pubmed_fetch.parse import parse_date


@pytest.fixture(scope="module")
def parsed():
    articles, skipped = parse_records(ET.parse(SAMPLE_XML).getroot())
    assert skipped == 0
    return {a["pmid"]: a for a in articles}


def test_all_fixture_records_parse(parsed):
    assert len(parsed) == 9
    assert all(a["url"] == f"https://pubmed.ncbi.nlm.nih.gov/{a['pmid']}/" for a in parsed.values())


def test_inline_markup_is_kept_in_abstract(parsed):
    article = parsed["28298962"]
    assert "by means of 18F-fluoro-2-deoxy-D-glucose positron emission tomography (FDG-PET)." in article["abstract"]
    assert (article["date"], article["date_precision"]) == ("2017-02-28", "day")
    assert (article["journal_abbrev"], article["volume"], article["issue"], article["pages"]) == (
        "World J Radiol",
        "9",
        "2",
        "27-33",
    )
    assert article["languages"] == ["eng"]


def test_ids_come_from_the_article_not_its_references(parsed):
    article = parsed["28298962"]
    assert (article["pmc_id"], article["doi"]) == ("PMC5334499", "10.4329/wjr.v9.i2.27")
    # This book cites many PMC articles but has no PMC ID of its own.
    assert parsed["33237689"]["pmc_id"] is None


def test_authors_orcid_affiliations_and_grants(parsed):
    article = parsed["35187501"]
    first = article["authors"][0]
    assert first["name"] == "Shalabi, Sundus F"
    assert first["orcid"] == "0000-0002-8440-2474"
    assert len(first["affiliations"]) == 3
    assert [a["position"] for a in article["authors"]] == list(range(1, 13))
    assert article["grants"][0] == {
        "grant_id": "R01 CA192914",
        "acronym": "CA",
        "agency": "NCI NIH HHS",
        "country": "United States",
    }
    assert article["epub_date"] == "2021-09-14"
    assert article["article_ids"]["mid"] == "NIHMS1772753"
    assert article["comments_corrections"] == [
        {"type": "CommentIn", "pmid": "37117630", "source": article["comments_corrections"][0]["source"]}
    ]


def test_collective_author(parsed):
    names = [a["name"] for a in parsed["42732105"]["authors"] if a["collective_name"]]
    assert len(names) == 6
    assert names[0] == "University of Oxford and Wytham Woods Genome Acquisition Lab"
    assert "Darwin Tree of Life Barcoding collective" in names
    assert parsed["42732105"]["publication_status"] == "epublish"


def test_retraction_and_erratum(parsed):
    retracted = parsed["36452530"]
    assert retracted["is_retracted"] is True
    assert {"type": "RetractionIn", "pmid": "37206796"}.items() <= retracted["comments_corrections"][0].items()
    erratum = parsed["40390958"]
    assert erratum["is_retracted"] is False
    assert erratum["comments_corrections"][0]["type"] == "ErratumFor"


def test_season_and_medline_dates(parsed):
    winter = parsed["10029841"]
    assert (winter["date"], winter["date_precision"], winter["date_raw"]) == ("1998", "year", "1998 Winter")
    medline = parsed["10115318"]
    assert (medline["date"], medline["month"], medline["date_precision"]) == ("1991-12", 12, "month")
    assert medline["date_raw"] == "1991 Dec 23-30"
    assert medline["doi"] is None and medline["pmc_id"] is None


def test_mesh_qualifiers_and_major_topics(parsed):
    heading = next(m for m in parsed["10029841"]["mesh"] if m["descriptor"] == "Disorders of Sex Development")
    assert heading["ui"] == "D012734"
    assert [(q["name"], q["major_topic"]) for q in heading["qualifiers"]] == [
        ("diagnosis", False),
        ("psychology", False),
        ("surgery", True),
    ]
    assert "Disorders of Sex Development" in parsed["10029841"]["mesh_terms"]


def test_structured_abstract_sections(parsed):
    article = parsed["38463030"]
    assert [s["label"] for s in article["abstract_sections"]] == ["BACKGROUND", "METHODS", "RESULTS", "CONCLUSION"]
    assert article["abstract"].startswith("BACKGROUND: ")


def test_book_record(parsed):
    book = parsed["33237689"]
    assert book["record_type"] == "book"
    assert book["title"] == "RFC1-Related Disorders"
    assert book["book_title"].startswith("GeneReviews")
    assert book["publisher"] == "University of Washington, Seattle"
    assert book["journal"] is None
    assert book["article_ids"]["bookaccession"] == "NBK564656"
    assert book["epub_date"] == "2020-11-25"
    assert [s["label"] for s in book["abstract_sections"]][:2] == ["CLINICAL CHARACTERISTICS", "DIAGNOSIS/TESTING"]


def test_missing_values_are_null_not_placeholder_strings():
    article = parse_article(
        ET.fromstring(
            "<PubmedArticle><MedlineCitation><PMID>1</PMID><Article/></MedlineCitation><PubmedData/></PubmedArticle>"
        )
    )
    for key in ("title", "journal", "date", "abstract", "doi", "pmc_id", "epub_date", "year"):
        assert article[key] is None, key
    assert article["authors"] == [] and article["authors_display"] == ""


def test_malformed_and_unknown_records_are_skipped():
    root = ET.fromstring(
        "<PubmedArticleSet><PubmedArticle><MedlineCitation/></PubmedArticle><DeleteCitation/></PubmedArticleSet>"
    )
    assert parse_records(root) == ([], 1)


@pytest.mark.parametrize(
    ("xml", "expected"),
    [
        ("<PubDate><Year>2024</Year></PubDate>", ("2024", "year", None)),
        ("<PubDate><Year>2024</Year><Month>11</Month></PubDate>", ("2024-11", "month", None)),
        ("<PubDate><Year>2024</Year><Month>Feb</Month><Day>5</Day></PubDate>", ("2024-02-05", "day", None)),
        ("<PubDate><Year>2024</Year><Day>5</Day></PubDate>", ("2024", "year", None)),
        ("<PubDate><MedlineDate>2023 Jan-Feb</MedlineDate></PubDate>", ("2023", "year", "2023 Jan-Feb")),
        ("<PubDate><MedlineDate>1998-1999</MedlineDate></PubDate>", ("1998", "year", "1998-1999")),
        ("<PubDate><MedlineDate>Unknown</MedlineDate></PubDate>", (None, None, "Unknown")),
    ],
)
def test_parse_date(xml, expected):
    result = parse_date(ET.fromstring(xml))
    assert (result["date"], result["date_precision"], result["date_raw"]) == expected
