from datetime import date

import pytest

from pubmed_fetch import PubMedError, QuerySpec, build_query, resolve_date_range


def test_build_query_or_within_and_across_categories():
    query = build_query(["Breast Neoplasms"], ["Smith J", "Lee K"], ["NIH"])
    assert (
        query == '("Breast Neoplasms"[MeSH Terms]) AND ("Smith J"[Author] OR "Lee K"[Author]) AND ("NIH"[Affiliation])'
    )
    assert build_query() == ""


def test_query_spec_all_filters():
    spec = QuerySpec(
        mesh_terms=["Carcinoma, Ductal, Breast"],
        title_abstract=["immunotherapy"],
        journals=["Nature"],
        publication_types=["Clinical Trial"],
        languages=["english"],
        raw_query="humans[mh]",
        exclude=["Review[pt]", "Comment[pt]"],
        mesh_noexp=True,
    )
    assert spec.build() == (
        '(("Carcinoma, Ductal, Breast"[MeSH Terms:noexp]) AND ("immunotherapy"[Title/Abstract]) AND '
        '("Nature"[Journal]) AND ("Clinical Trial"[Publication Type]) AND ("english"[Language]) AND (humans[mh])) '
        "NOT ((Review[pt]) OR (Comment[pt]))"
    )
    assert spec.filters()["mesh_noexp"] is True


def test_query_spec_quotes_and_ignores_blanks():
    assert QuerySpec(authors=['O"Brien K', "  "]).build() == '("OBrien K"[Author])'
    assert QuerySpec(raw_query="  ").is_empty()
    # Exclusions alone don't make a search.
    assert QuerySpec(exclude=["Review[pt]"]).is_empty()


def test_resolve_date_range():
    today = date(2024, 3, 31)
    assert resolve_date_range(30, today=today) == (date(2024, 3, 1), today)
    assert resolve_date_range(30, start_date=date(2024, 1, 1), today=today) == (date(2024, 1, 1), today)
    assert resolve_date_range(10, end_date=date(2024, 1, 11)) == (date(2024, 1, 1), date(2024, 1, 11))
    with pytest.raises(PubMedError):
        resolve_date_range(start_date=date(2024, 2, 1), end_date=date(2024, 1, 1))
