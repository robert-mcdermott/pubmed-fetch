import json
import xml.etree.ElementTree as ET

import pytest
import requests

import pubmed_fetch as pf

ARTICLE_XML = """
<PubmedArticle>
  <MedlineCitation>
    <PMID Version="1">12345</PMID>
    <Article>
      <Journal>
        <JournalIssue><PubDate><Year>2024</Year><Month>Feb</Month><Day>5</Day></PubDate></JournalIssue>
        <Title>Journal of Tests</Title>
      </Journal>
      <ArticleTitle>Role of <i>BRCA1</i> in cancer</ArticleTitle>
      <ELocationID EIdType="doi">10.1/eloc</ELocationID>
      <Abstract>
        <AbstractText Label="BACKGROUND">Imaging with <sup>18</sup>F-FDG PET.</AbstractText>
        <AbstractText Label="RESULTS">It worked.</AbstractText>
      </Abstract>
      <AuthorList>
        <Author><LastName>Smith</LastName><ForeName>Jane</ForeName></Author>
        <Author><LastName>Lee</LastName></Author>
        <Author><CollectiveName>Test Consortium</CollectiveName></Author>
      </AuthorList>
      <PublicationTypeList><PublicationType>Journal Article</PublicationType></PublicationTypeList>
    </Article>
    <MeshHeadingList>
      <MeshHeading><DescriptorName>Breast Neoplasms</DescriptorName></MeshHeading>
    </MeshHeadingList>
    <KeywordList><Keyword>BRCA1</Keyword></KeywordList>
  </MedlineCitation>
  <PubmedData>
    <ArticleIdList>
      <ArticleId IdType="pubmed">12345</ArticleId>
      {own_ids}
    </ArticleIdList>
    <ReferenceList>
      <Reference>
        <ArticleIdList>
          <ArticleId IdType="doi">10.9/reference</ArticleId>
          <ArticleId IdType="pmc">PMC999</ArticleId>
        </ArticleIdList>
      </Reference>
    </ReferenceList>
  </PubmedData>
</PubmedArticle>
"""


def make_article(own_ids=""):
    return ET.fromstring(ARTICLE_XML.replace("{own_ids}", own_ids))


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


class FakeSession:
    """Routes requests to handlers keyed by URL substring and records calls."""

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


@pytest.fixture(autouse=True)
def no_rate_limit_sleep(monkeypatch):
    monkeypatch.setattr(pf.time, "sleep", lambda seconds: None)


def make_searcher(routes, **kwargs):
    return pf.PubMedSearcher(session=FakeSession(routes), **kwargs)


def esearch_response(count, webenv="WE", query_key="1"):
    return lambda url, params: FakeResponse(
        f"<eSearchResult><Count>{count}</Count><QueryKey>{query_key}</QueryKey>"
        f"<WebEnv>{webenv}</WebEnv><IdList/></eSearchResult>".encode()
    )


def test_parse_arguments_mesh_and_author():
    args = pf.parse_arguments(
        ["--mesh-terms", "Breast Neoplasms", "Ovarian Neoplasms", "--author", "Gilbert P", "--days", "45"]
        + ["--max-results", "25"]
    )
    assert args.mesh_terms == ["Breast Neoplasms", "Ovarian Neoplasms"]
    assert args.author == ["Gilbert P"]
    assert args.days == 45
    assert args.max_results == 25


def test_parse_arguments_rejects_non_positive_days():
    with pytest.raises(SystemExit):
        pf.parse_arguments(["--mesh-terms", "X", "--days", "0"])


def test_build_query():
    query = pf.build_query(["Breast Neoplasms"], ["Smith J", "Lee K"], ["NIH"])
    assert (
        query == '("Breast Neoplasms"[MeSH Terms]) AND ("Smith J"[Author] OR "Lee K"[Author]) AND ("NIH"[Affiliation])'
    )
    assert pf.build_query() == ""


def test_build_query_and_params():
    searcher = make_searcher({"esearch.fcgi": esearch_response(0)}, email="you@example.com", api_key="KEY")
    articles = searcher.search_pubmed(
        mesh_terms=["Breast Neoplasms"], authors=["Smith J", "Lee K"], organizations=["NIH"], days_back=90
    )

    assert articles == []
    method, url, params = searcher.session.calls[0]
    assert "esearch.fcgi" in url
    assert '("Smith J"[Author] OR "Lee K"[Author])' in params["term"]
    assert params["datetype"] == "pdat"
    assert params["usehistory"] == "y"
    assert params["tool"] == searcher.tool
    assert params["email"] == "you@example.com"
    assert params["api_key"] == "KEY"
    assert searcher.search_info["authors"] == ["Smith J", "Lee K"]
    assert searcher.search_info["total_found"] == 0


def test_search_pages_through_history_and_caps_results(caplog):
    efetch_calls = []

    def efetch(url, params):
        efetch_calls.append(params)
        return FakeResponse(b"<PubmedArticleSet>" + ET.tostring(make_article()) + b"</PubmedArticleSet>")

    searcher = make_searcher({"esearch.fcgi": esearch_response(1000, webenv="ENV"), "efetch.fcgi": efetch})
    searcher.batch_size = 100
    articles = searcher.search_pubmed(mesh_terms=["X"], max_results=250)

    assert [(c["retstart"], c["retmax"]) for c in efetch_calls] == [(0, 100), (100, 100), (200, 50)]
    assert all(c["WebEnv"] == "ENV" and c["query_key"] == "1" for c in efetch_calls)
    assert len(articles) == 3
    assert "Only the first 250 of 1000" in caplog.text


def test_search_raises_on_network_error():
    def boom(url, params):
        raise requests.ConnectionError("down")

    searcher = make_searcher({"esearch.fcgi": boom})
    with pytest.raises(pf.PubMedError):
        searcher.search_pubmed(mesh_terms=["X"])


def test_search_raises_on_eutils_error():
    searcher = make_searcher(
        {"esearch.fcgi": lambda u, p: FakeResponse(b"<eSearchResult><ERROR>bad</ERROR></eSearchResult>")}
    )
    with pytest.raises(pf.PubMedError, match="bad"):
        searcher.search_pubmed(mesh_terms=["X"])


def test_failed_efetch_batch_is_skipped():
    responses = iter(
        [
            FakeResponse(status=500),
            FakeResponse(b"<PubmedArticleSet>" + ET.tostring(make_article()) + b"</PubmedArticleSet>"),
        ]
    )
    searcher = make_searcher({"efetch.fcgi": lambda u, p: next(responses)})
    searcher.batch_size = 1
    assert len(searcher.fetch_article_details(["1", "2"])) == 1


def test_parse_article_handles_inline_markup_and_authors():
    article = pf.PubMedSearcher(session=FakeSession({})).parse_article(make_article())
    assert article["pmid"] == "12345"
    assert article["title"] == "Role of BRCA1 in cancer"
    assert article["abstract"] == "BACKGROUND: Imaging with 18F-FDG PET. RESULTS: It worked."
    assert article["authors"] == ["Smith, Jane", "Lee", "Test Consortium"]
    assert article["date"] == "2024-02-05"
    assert article["mesh_terms"] == ["Breast Neoplasms"]
    assert article["keywords"] == ["BRCA1"]
    assert article["publication_types"] == ["Journal Article"]


def test_parse_article_ignores_reference_ids():
    searcher = pf.PubMedSearcher(session=FakeSession({}))
    article = searcher.parse_article(make_article())
    assert article["pmc_id"] == ""
    assert article["doi"] == "10.1/eloc"  # falls back to ELocationID, not the cited reference's DOI

    own = '<ArticleId IdType="doi">10.1/own</ArticleId><ArticleId IdType="pmc">PMC42</ArticleId>'
    article = searcher.parse_article(make_article(own))
    assert article["doi"] == "10.1/own"
    assert article["pmc_id"] == "PMC42"


@pytest.mark.parametrize(
    ("xml", "expected"),
    [
        ("<PubDate><Year>2024</Year></PubDate>", "2024"),
        ("<PubDate><Year>2024</Year><Month>11</Month></PubDate>", "2024-11"),
        ("<PubDate><Year>2024</Year><Season>Spring</Season></PubDate>", "2024"),
        ("<PubDate><MedlineDate>2023 Jan-Feb</MedlineDate></PubDate>", "2023 Jan-Feb"),
    ],
)
def test_format_pub_date(xml, expected):
    assert pf._format_pub_date(ET.fromstring(xml)) == expected


S3_LISTING = """<?xml version="1.0" encoding="UTF-8"?>
<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/"><Name>pmc-oa-opendata</Name>
<Prefix>{pmcid}.</Prefix>{prefixes}</ListBucketResult>"""


def s3_routes(datasets):
    """datasets maps a PMCID to a list of metadata dicts (one per version)."""

    def listing(url, params):
        pmcid = params["prefix"].rstrip(".")
        prefixes = "".join(
            f"<CommonPrefixes><Prefix>{pmcid}.{m['version']}/</Prefix></CommonPrefixes>"
            for m in datasets.get(pmcid, [])
        )
        return FakeResponse(S3_LISTING.format(pmcid=pmcid, prefixes=prefixes).encode())

    def metadata(url, params):
        version = url.rsplit("/", 1)[1].removesuffix(".json")
        pmcid, num = version.rsplit(".", 1)
        return FakeResponse(json_data=next(m for m in datasets[pmcid] if str(m["version"]) == num))

    return {"/metadata/": metadata, "pmc-oa-opendata.s3.amazonaws.com/": listing}


def test_add_fulltext_info_uses_pmc_cloud_metadata():
    datasets = {
        "PMC1": [
            {"version": 1, "is_pmc_openaccess": False, "is_manuscript": True, "license_code": "TDM"},
            {
                "version": 2,
                "is_pmc_openaccess": True,
                "is_manuscript": False,
                "license_code": "CC BY",
                "pdf_url": "s3://pmc-oa-opendata/PMC1.2/PMC1.2.pdf?md5=abc",
                "xml_url": "s3://pmc-oa-opendata/PMC1.2/PMC1.2.xml?md5=def",
            },
        ],
    }
    searcher = make_searcher(s3_routes(datasets))
    articles = [
        {"pmid": "1", "pmc_id": "PMC1", "doi": "10.1/a"},
        {"pmid": "2", "pmc_id": "2", "doi": ""},
        {"pmid": "3", "pmc_id": "", "doi": "10.1/c"},
        {"pmid": "4", "pmc_id": "", "doi": ""},
    ]
    searcher.add_fulltext_info(articles)

    open_ft = articles[0]["fulltext"]
    assert open_ft["access_type"] == "open"
    assert open_ft["license"] == "CC BY"
    assert open_ft["url"] == "https://pmc.ncbi.nlm.nih.gov/articles/PMC1/"
    assert open_ft["download_links"] == [
        {"format": "pdf", "url": "https://pmc-oa-opendata.s3.amazonaws.com/PMC1.2/PMC1.2.pdf"},
        {"format": "xml", "url": "https://pmc-oa-opendata.s3.amazonaws.com/PMC1.2/PMC1.2.xml"},
    ]
    assert articles[1]["fulltext"] == {
        "url": "https://pmc.ncbi.nlm.nih.gov/articles/PMC2/",
        "source": "PMC",
        "access_type": "free",
    }
    assert articles[2]["fulltext"]["access_type"] == "unknown"
    assert articles[2]["fulltext"]["url"] == "https://doi.org/10.1/c"
    assert articles[3]["fulltext"] is None


def test_add_fulltext_info_tolerates_lookup_failures(caplog):
    def fail(url, params):
        raise requests.ConnectionError("down")

    searcher = make_searcher({"amazonaws.com": fail})
    articles = [{"pmid": "1", "pmc_id": "PMC1", "doi": ""}]
    searcher.add_fulltext_info(articles)
    assert articles[0]["fulltext"]["access_type"] == "free"
    assert "Could not check open-access status for 1" in caplog.text


def test_load_config_and_precedence(tmp_path, monkeypatch):
    config_file = tmp_path / "c.ini"
    config_file.write_text("[search]\nmesh_terms = A, B ,\ndays = 60\nmax_results = 5\nemail = cfg@example.com\n")
    config = pf.load_config(config_file)
    assert config == {"mesh_terms": ["A", "B"], "days": 60, "max_results": 5, "email": "cfg@example.com"}

    monkeypatch.delenv("NCBI_EMAIL", raising=False)
    monkeypatch.setenv("NCBI_API_KEY", "envkey")
    # Explicitly passing the default value must still override the config file.
    settings = pf.resolve_settings(pf.parse_arguments(["--days", "30"]), config)
    assert settings["days"] == 30
    assert settings["max_results"] == 5
    assert settings["mesh_terms"] == ["A", "B"]
    assert settings["email"] == "cfg@example.com"
    assert settings["api_key"] == "envkey"
    assert settings["output_file"] == "pubmed_articles.json"


def test_create_sample_config_does_not_overwrite(tmp_path):
    target = tmp_path / "sample.ini"
    assert pf.create_sample_config(target)
    assert "mesh_terms" in pf.load_config(target)
    assert not pf.create_sample_config(target)


def test_save_to_json_includes_search_info(tmp_path):
    searcher = pf.PubMedSearcher(session=FakeSession({}))
    searcher.search_info = {"query": "q", "authors": ["Smith J"]}
    out = tmp_path / "out.json"
    searcher.save_to_json([{"pmid": "1"}], filename=out)
    data = json.loads(out.read_text())
    assert data["search_info"]["query"] == "q"
    assert data["search_info"]["authors"] == ["Smith J"]
    assert data["search_info"]["total_articles"] == 1
    assert data["articles"] == [{"pmid": "1"}]


def test_main_requires_filters():
    assert pf.main([]) == 1


def test_main_missing_config(tmp_path):
    assert pf.main(["--config", str(tmp_path / "missing.ini")]) == 1


def test_main_returns_error_when_search_fails(monkeypatch):
    def fail(self, **kwargs):
        raise pf.PubMedError("down")

    monkeypatch.setattr(pf.PubMedSearcher, "search_pubmed", fail)
    assert pf.main(["--mesh-terms", "X", "-q"]) == 1
