import csv
import json
import xml.etree.ElementTree as ET
from datetime import date

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

    def iter_content(self, chunk_size=1):
        yield self.content

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


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


ARTICLE_SET = b"<PubmedArticleSet>" + ET.tostring(make_article()) + b"</PubmedArticleSet>"


def test_failed_efetch_batch_is_skipped(caplog):
    def efetch(url, params):
        return FakeResponse(status=500) if params["id"] == "1" else FakeResponse(ARTICLE_SET)

    searcher = make_searcher({"efetch.fcgi": efetch})
    searcher.batch_size = 1
    assert len(searcher.fetch_article_details(["1", "2"])) == 1
    assert "Batch 1/2 failed" in caplog.text


def test_efetch_batch_is_retried_once():
    responses = iter([FakeResponse(status=400), FakeResponse(ARTICLE_SET)])
    searcher = make_searcher({"efetch.fcgi": lambda u, p: next(responses)})
    assert len(searcher.fetch_article_details(["1"])) == 1


def test_all_efetch_batches_failing_raises():
    searcher = make_searcher({"efetch.fcgi": lambda u, p: FakeResponse(status=400)})
    with pytest.raises(pf.PubMedError, match="Could not fetch article details"):
        searcher.fetch_article_details(["1"])


def test_search_uses_explicit_date_range():
    searcher = make_searcher({"esearch.fcgi": esearch_response(0)})
    searcher.search_pubmed(mesh_terms=["X"], start_date=date(2024, 1, 1), end_date=date(2024, 6, 30))
    params = searcher.session.calls[0][2]
    assert (params["mindate"], params["maxdate"]) == ("2024/01/01", "2024/06/30")
    assert searcher.search_info["date_range"] == {"start_date": "2024-01-01", "end_date": "2024-06-30"}


def test_resolve_date_range():
    today = date(2024, 3, 31)
    assert pf.resolve_date_range(30, today=today) == (date(2024, 3, 1), today)
    assert pf.resolve_date_range(30, start_date=date(2024, 1, 1), today=today) == (date(2024, 1, 1), today)
    assert pf.resolve_date_range(10, end_date=date(2024, 1, 11)) == (date(2024, 1, 1), date(2024, 1, 11))
    with pytest.raises(pf.PubMedError):
        pf.resolve_date_range(start_date=date(2024, 2, 1), end_date=date(2024, 1, 1))


def test_parse_arguments_dates():
    args = pf.parse_arguments(["--mesh-terms", "X", "--start-date", "2024-01-01", "--end-date", "2024-02-01"])
    assert (args.start_date, args.end_date) == (date(2024, 1, 1), date(2024, 2, 1))
    for bad in (["--start-date", "01/02/2024"], ["--days", "5", "--start-date", "2024-01-01"]):
        with pytest.raises(SystemExit):
            pf.parse_arguments(["--mesh-terms", "X", *bad])


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
    assert settings["output_file"] is None
    assert settings["download_formats"] == ["pdf"]


def test_load_config_dates_formats_and_downloads(tmp_path):
    config_file = tmp_path / "c.ini"
    config_file.write_text(
        "[search]\nstart_date = 2024-01-01\nend_date = 2024-12-31\nformat = CSV\n"
        "download_dir = papers\ndownload_formats = pdf, xml\n"
    )
    config = pf.load_config(config_file)
    assert config == {
        "start_date": date(2024, 1, 1),
        "end_date": date(2024, 12, 31),
        "format": "csv",
        "download_dir": "papers",
        "download_formats": ["pdf", "xml"],
    }
    # --days on the command line replaces the config file's start_date but keeps its end_date.
    settings = pf.resolve_settings(pf.parse_arguments(["--days", "7"]), config)
    assert (settings["start_date"], settings["end_date"], settings["days"]) == (None, date(2024, 12, 31), 7)

    config_file.write_text("[search]\nformat = xlsx\n")
    with pytest.raises(ValueError, match="format"):
        pf.load_config(config_file)


@pytest.mark.parametrize(
    ("filename", "fmt", "expected"),
    [
        (None, None, ("pubmed_articles.json", "json")),
        (None, "csv", ("pubmed_articles.csv", "csv")),
        ("out.JSONL", None, ("out.JSONL", "jsonl")),
        ("out.txt", None, ("out.txt", "json")),
        ("out.json", "csv", ("out.json", "csv")),
    ],
)
def test_resolve_output(filename, fmt, expected):
    assert pf.resolve_output(filename, fmt) == expected


SAMPLE_ARTICLES = [
    {
        "pmid": "1",
        "title": 'A "quoted", title',
        "authors": ["Smith, Jane", "Lee"],
        "mesh_terms": ["X"],
        "keywords": [],
        "publication_types": ["Review"],
        "abstract": "Line one\nline two",
        "fulltext": {"url": "https://pmc/1", "access_type": "open", "license": "CC BY"},
    },
    {"pmid": "2", "title": "Second", "authors": [], "fulltext": None},
]


def test_save_results_csv(tmp_path):
    out = tmp_path / "out.csv"
    pf.PubMedSearcher(session=FakeSession({})).save_results(SAMPLE_ARTICLES, filename=out)
    rows = list(csv.DictReader(out.open(encoding="utf-8", newline="")))
    assert list(rows[0]) == pf.CSV_FIELDS
    assert rows[0]["title"] == 'A "quoted", title'
    assert rows[0]["authors"] == "Smith, Jane; Lee"
    assert rows[0]["abstract"] == "Line one\nline two"
    assert (rows[0]["access_type"], rows[0]["license"], rows[0]["fulltext_url"]) == ("open", "CC BY", "https://pmc/1")
    assert rows[1]["access_type"] == ""


def test_save_results_jsonl(tmp_path):
    out = tmp_path / "out.jsonl"
    pf.PubMedSearcher(session=FakeSession({})).save_results(SAMPLE_ARTICLES, filename=out)
    lines = out.read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["pmid"] for line in lines] == ["1", "2"]


def test_download_fulltext(tmp_path):
    base = "https://pmc-oa-opendata.s3.amazonaws.com/PMC1.1/"

    def download(url, params):
        return FakeResponse(status=404) if url.endswith("missing.pdf") else FakeResponse(b"data:" + url.encode())

    links = [
        {"format": "pdf", "url": base + "PMC1.1.pdf"},
        {"format": "xml", "url": base + "PMC1.1.xml"},
        {"format": "pdf", "url": base + "existing.pdf"},
        {"format": "pdf", "url": base + "missing.pdf"},
    ]
    articles = [{"pmid": "1", "fulltext": {"download_links": links}}, {"pmid": "2", "fulltext": None}]
    (tmp_path / "existing.pdf").write_bytes(b"old")
    searcher = make_searcher({"amazonaws.com": download})

    assert searcher.download_fulltext(articles, tmp_path, formats=["pdf"]) == (1, 1)
    assert (tmp_path / "PMC1.1.pdf").read_bytes() == b"data:" + (base + "PMC1.1.pdf").encode()
    assert (tmp_path / "existing.pdf").read_bytes() == b"old"
    assert not (tmp_path / "PMC1.1.xml").exists()
    assert not list(tmp_path.glob("*.part"))
    assert links[0]["path"] == str(tmp_path / "PMC1.1.pdf")
    assert links[2]["path"] == str(tmp_path / "existing.pdf")
    assert "path" not in links[1] and "path" not in links[3]


def test_main_rejects_download_without_fulltext(tmp_path):
    assert pf.main(["--mesh-terms", "X", "--no-fulltext", "--download-fulltext", str(tmp_path), "-q"]) == 1


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
