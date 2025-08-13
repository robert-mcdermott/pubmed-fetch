import pubmed_fetch as pf


def test_parse_arguments_mesh_and_author(monkeypatch):
    # Simulate CLI: mesh terms + author + days
    test_argv = [
        "prog",
        "--mesh-terms", "Breast Neoplasms", "Ovarian Neoplasms",
        "--author", "Gilbert P",
        "--days", "45",
        "--max-results", "25",
    ]
    monkeypatch.setattr(pf.sys, "argv", test_argv)
    args = pf.parse_arguments()
    assert args.mesh_terms == ["Breast Neoplasms", "Ovarian Neoplasms"]
    assert args.author == ["Gilbert P"]
    assert args.days == 45
    assert args.max_results == 25


def test_build_query_and_params(monkeypatch):
    # Build a searcher and intercept requests.get to capture params
    captured = {}

    def fake_get(url, params=None, **kwargs):
        captured["url"] = url
        captured["params"] = params or {}
        class R:
            status_code = 200
            def raise_for_status(self):
                return None
            content = b"""<?xml version='1.0' encoding='UTF-8'?>
                <eSearchResult>
                  <Count>0</Count>
                  <IdList></IdList>
                </eSearchResult>"""
        return R()

    monkeypatch.setattr(pf.requests, "get", fake_get)

    searcher = pf.PubMedSearcher()
    searcher.email = "you@example.com"

    # Compose a query using multiple categories
    searcher.search_pubmed(
        mesh_terms=["Breast Neoplasms"],
        authors=["Smith J", "Lee K"],
        organizations=["NIH"],
        days_back=90,
        max_results=50,
    )

    assert "esearch.fcgi" in captured["url"]
    term = captured["params"]["term"]
    # OR within categories
    assert "Smith J[Author]" in term and "Lee K[Author]" in term
    assert "(Smith J[Author] OR Lee K[Author])" in term
    # AND across categories
    assert "[MeSH Terms]" in term and "[Affiliation]" in term and " AND " in term
    assert captured["params"]["retmax"] == 50
    assert captured["params"]["datetype"] == "pdat"
    assert captured["params"]["tool"] == searcher.tool
    assert captured["params"]["email"] == searcher.email

