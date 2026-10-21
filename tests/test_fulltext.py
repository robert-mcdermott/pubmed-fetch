import requests
from conftest import FakeResponse, FakeSession

from pubmed_fetch import PMCFullText, add_icite_metrics

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


def test_annotate_uses_pmc_cloud_metadata():
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
    articles = [
        {"pmid": "1", "pmc_id": "PMC1", "doi": "10.1/a"},
        {"pmid": "2", "pmc_id": "2", "doi": None},
        {"pmid": "3", "pmc_id": None, "doi": "10.1/c"},
        {"pmid": "4", "pmc_id": None, "doi": None, "article_ids": {"bookaccession": "NBK1"}},
        {"pmid": "5", "pmc_id": None, "doi": None},
    ]
    assert PMCFullText(FakeSession(s3_routes(datasets))).annotate(articles) == 0

    assert articles[0]["fulltext"] == {
        "url": "https://pmc.ncbi.nlm.nih.gov/articles/PMC1/",
        "source": "PMC",
        "access_type": "open",
        "oa_checked": True,
        "license": "CC BY",
        "is_manuscript": False,
        "retracted": False,
        "download_links": [
            {"format": "pdf", "url": "https://pmc-oa-opendata.s3.amazonaws.com/PMC1.2/PMC1.2.pdf", "path": None},
            {"format": "xml", "url": "https://pmc-oa-opendata.s3.amazonaws.com/PMC1.2/PMC1.2.xml", "path": None},
        ],
    }
    assert (articles[1]["fulltext"]["access_type"], articles[1]["fulltext"]["oa_checked"]) == ("free", True)
    assert articles[1]["fulltext"]["url"] == "https://pmc.ncbi.nlm.nih.gov/articles/PMC2/"
    assert (articles[2]["fulltext"]["access_type"], articles[2]["fulltext"]["url"]) == (
        "unknown",
        "https://doi.org/10.1/c",
    )
    assert articles[3]["fulltext"]["url"] == "https://www.ncbi.nlm.nih.gov/books/NBK1/"
    assert articles[4]["fulltext"] is None


def test_annotate_tolerates_lookup_failures(caplog):
    def fail(url, params):
        raise requests.ConnectionError("down")

    articles = [{"pmid": "1", "pmc_id": "PMC1", "doi": None}]
    assert PMCFullText(FakeSession({"amazonaws.com": fail})).annotate(articles) == 1
    assert articles[0]["fulltext"]["access_type"] == "free"
    assert articles[0]["fulltext"]["oa_checked"] is False
    assert "Could not check open-access status for 1" in caplog.text


def test_download(tmp_path):
    base = "https://pmc-oa-opendata.s3.amazonaws.com/PMC1.1/"

    def download(url, params):
        return FakeResponse(status=404) if url.endswith("missing.pdf") else FakeResponse(b"data:" + url.encode())

    links = [
        {"format": "pdf", "url": base + "PMC1.1.pdf", "path": None},
        {"format": "xml", "url": base + "PMC1.1.xml", "path": None},
        {"format": "pdf", "url": base + "existing.pdf", "path": None},
        {"format": "pdf", "url": base + "missing.pdf", "path": None},
    ]
    articles = [{"pmid": "1", "fulltext": {"download_links": links}}, {"pmid": "2", "fulltext": None}]
    (tmp_path / "existing.pdf").write_bytes(b"old")

    assert PMCFullText(FakeSession({"amazonaws.com": download})).download(articles, tmp_path, ["pdf"]) == (1, 1)
    assert (tmp_path / "PMC1.1.pdf").read_bytes() == b"data:" + (base + "PMC1.1.pdf").encode()
    assert (tmp_path / "existing.pdf").read_bytes() == b"old"
    assert not (tmp_path / "PMC1.1.xml").exists()
    assert not list(tmp_path.glob("*.part"))
    assert [link["path"] for link in links] == [
        str(tmp_path / "PMC1.1.pdf"),
        None,
        str(tmp_path / "existing.pdf"),
        None,
    ]


def test_icite_metrics():
    def icite(url, params):
        assert params["pmids"] == "1,2"
        assert params["fl"].startswith("pmid,citation_count")
        return FakeResponse(json_data={"data": [{"pmid": 1, "citation_count": 15, "relative_citation_ratio": 0.4}]})

    articles = [{"pmid": "1"}, {"pmid": "2"}]
    assert add_icite_metrics(FakeSession({"icite.od.nih.gov": icite}), articles) == 0
    assert articles[0]["metrics"]["source"] == "icite"
    assert articles[0]["metrics"]["citation_count"] == 15
    assert articles[0]["metrics"]["nih_percentile"] is None
    assert articles[1]["metrics"] is None


def test_icite_failures_are_counted(caplog):
    articles = [{"pmid": "1", "metrics": None}]
    assert add_icite_metrics(FakeSession({"icite": lambda u, p: FakeResponse(status=503)}), articles) == 1
    assert articles[0]["metrics"] is None
    assert "Could not fetch iCite metrics for 1" in caplog.text
