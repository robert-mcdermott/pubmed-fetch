import json
from datetime import date

import pytest

from pubmed_fetch import cli
from pubmed_fetch.cli import build_options, load_config, parse_arguments, parse_list, read_pmids, resolve_settings


def test_parse_arguments():
    args = parse_arguments(
        ["--mesh-terms", "Breast Neoplasms", "Ovarian Neoplasms", "--author", "Gilbert P", "--days", "45"]
        + ["--journal", "Nature", "--exclude", "Review[pt]", "--mesh-noexp", "--no-fulltext", "--icite"]
    )
    assert args.mesh_terms == ["Breast Neoplasms", "Ovarian Neoplasms"]
    assert args.authors == ["Gilbert P"]
    assert args.journals == ["Nature"]
    assert (args.days, args.mesh_noexp, args.fulltext, args.icite, args.merge) == (45, True, False, True, None)


def test_max_results_all(tmp_path):
    assert parse_arguments(["--mesh-terms", "X", "--max-results", "ALL"]).max_results == "all"
    options = build_options(resolve_settings(parse_arguments(["--mesh-terms", "X", "--max-results", "all"]), {}))
    assert options.max_results is None
    config_file = tmp_path / "c.ini"
    config_file.write_text("[search]\nmax_results = all\n")
    assert load_config(str(config_file)) == {"max_results": "all"}
    config_file.write_text("[search]\nmax_results = lots\n")
    with pytest.raises(ValueError):
        load_config(str(config_file))
    for bad in ("0", "lots"):
        with pytest.raises(SystemExit):
            parse_arguments(["--mesh-terms", "X", "--max-results", bad])


def test_repeated_list_options_accumulate():
    args = parse_arguments(
        ["--author", "Smith J", "--author", "Lee K", "Kim S", "--mesh-terms", "A", "--mesh-terms", "B"]
        + ["--download-formats", "pdf", "--download-formats", "xml"]
    )
    assert args.authors == ["Smith J", "Lee K", "Kim S"]
    assert args.mesh_terms == ["A", "B"]
    assert args.download_formats == ["pdf", "xml"]
    # Unset lists stay None, so config file values still apply.
    assert args.journals is None and args.exclude is None


@pytest.mark.parametrize(
    "bad",
    [
        ["--days", "0"],
        ["--start-date", "01/02/2024"],
        ["--days", "5", "--start-date", "2024-01-01"],
        ["--format", "xlsx"],
        ["--date-type", "xdat"],
    ],
)
def test_parse_arguments_rejects_invalid_values(bad):
    with pytest.raises(SystemExit):
        parse_arguments(["--mesh-terms", "X", *bad])


def test_parse_list_supports_quotes_and_lines():
    assert parse_list('"Carcinoma, Ductal, Breast", Breast Neoplasms') == [
        "Carcinoma, Ductal, Breast",
        "Breast Neoplasms",
    ]
    assert parse_list("\n  A\n  B, C\n") == ["A", "B", "C"]


def test_load_config(tmp_path, caplog):
    config_file = tmp_path / "c.ini"
    config_file.write_text(
        "[search]\n"
        'mesh_terms = "Neoplasms, Glandular and Epithelial", B\n'
        "journals =\n  Nature\n  Science\n"
        "days = 60\nmax_results = 5\nemail = cfg@example.com\n"
        "start_date = 2024-01-01\nend_date = 2024-12-31\nformat = CSV\ndate_type = EDAT\n"
        "download_dir = papers\ndownload_formats = pdf, xml\nicite = yes\nfulltext = no\n"
        "query = humans[mh]\nexclude = Review[pt]\ntypo_key = 1\n"
    )
    config = load_config(str(config_file))
    assert config == {
        "mesh_terms": ["Neoplasms, Glandular and Epithelial", "B"],
        "journals": ["Nature", "Science"],
        "exclude": ["Review[pt]"],
        "download_formats": ["pdf", "xml"],
        "days": 60,
        "max_results": 5,
        "icite": True,
        "fulltext": False,
        "start_date": date(2024, 1, 1),
        "end_date": date(2024, 12, 31),
        "query": "humans[mh]",
        "date_type": "edat",
        "format": "csv",
        "download_dir": "papers",
        "email": "cfg@example.com",
    }
    assert "Ignoring unknown setting 'typo_key'" in caplog.text

    config_file.write_text("[search]\nformat = xlsx\n")
    with pytest.raises(ValueError, match="format"):
        load_config(str(config_file))


def test_settings_precedence(monkeypatch):
    config = {"mesh_terms": ["A"], "days": 60, "max_results": 5, "email": "cfg@example.com", "icite": True}
    monkeypatch.setenv("NCBI_API_KEY", "envkey")
    monkeypatch.setenv("NCBI_EMAIL", "env@example.com")
    # Explicitly passing the default value must still override the config file.
    settings = resolve_settings(parse_arguments(["--days", "30", "--email", "cli@example.com"]), config)
    assert (settings["days"], settings["max_results"], settings["mesh_terms"]) == (30, 5, ["A"])
    assert (settings["email"], settings["api_key"], settings["icite"]) == ("cli@example.com", "envkey", True)
    assert settings["output_file"] is None and settings["download_formats"] == ["pdf"]


def test_cli_days_replaces_config_start_date():
    config = {"start_date": date(2024, 1, 1), "end_date": date(2024, 12, 31)}
    settings = resolve_settings(parse_arguments(["--days", "7"]), config)
    assert (settings["start_date"], settings["end_date"], settings["days"]) == (None, date(2024, 12, 31), 7)


def test_build_options(tmp_path):
    pmids = tmp_path / "pmids.txt"
    pmids.write_text("# my list\n123, 456\n789\n")
    args = parse_arguments(["--pmids", str(pmids), "--output", "x.csv", "--merge"])
    options = build_options(resolve_settings(args, {}), quiet=True)
    assert options.pmids == ["123", "456", "789"]
    assert (options.mode, options.output_file, options.merge, options.display) == ("pmids", "x.csv", True, False)


def test_read_pmids_rejects_garbage(tmp_path):
    bad = tmp_path / "bad.txt"
    bad.write_text("123 abc")
    with pytest.raises(ValueError, match="abc"):
        read_pmids(str(bad))


def test_create_sample_config(tmp_path):
    target = tmp_path / "sample.ini"
    assert cli.create_sample_config(str(target))
    assert load_config(str(target))["mesh_terms"] == ["Endometrial Neoplasms", "Ovarian Neoplasms"]
    assert not cli.create_sample_config(str(target))


def test_repo_sample_config_matches_template():
    with open("sample_config.ini", encoding="utf-8") as f:
        assert f.read() == cli.SAMPLE_CONFIG
    assert load_config("sample_config.ini")["days"] == 30


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["--exclude", "Review[pt]"],
        ["--mesh-terms", "X", "--pmids", "-"],
        ["--mesh-terms", "X", "--no-fulltext", "--download-fulltext", "d"],
        ["--mesh-terms", "X", "--output", "-", "--format", "sqlite"],
        ["--mesh-terms", "X", "--merge", "--resume"],
        ["--pmids", "-", "--since-last-run", "s.json"],
        ["--mesh-terms", "X", "--start-date", "2024-01-01", "--since-last-run", "s.json"],
        ["--config", "missing.ini"],
    ],
)
def test_main_rejects_invalid_usage(argv, monkeypatch):
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO("1"))
    assert cli.main([*argv, "-q"]) == 1


def test_version(capsys):
    with pytest.raises(SystemExit):
        cli.main(["--version"])
    assert capsys.readouterr().out.startswith("pubmed-fetch ")


def test_summary_written(tmp_path, monkeypatch):
    from conftest import FakeSession, efetch_handler, esearch_handler

    monkeypatch.setattr(
        "pubmed_fetch.client.make_session",
        lambda: FakeSession({"esearch.fcgi": esearch_handler(2), "efetch.fcgi": efetch_handler()}),
    )
    out, summary = tmp_path / "out.jsonl", tmp_path / "summary.json"
    code = cli.main(["--mesh-terms", "X", "--no-fulltext", "-q", "--output", str(out), "--summary", str(summary)])
    assert code == 0
    info = json.loads(summary.read_text())
    assert (info["total_articles"], info["complete"], info["source"]) == (2, True, "search")
