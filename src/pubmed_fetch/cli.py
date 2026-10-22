"""Command-line interface."""

from __future__ import annotations

import argparse
import configparser
import csv
import logging
import os
import re
import sqlite3
import sys
from datetime import date
from typing import Any

from ._meta import PubMedError, __version__
from .fulltext import DOWNLOAD_FORMATS
from .outputs import OUTPUT_FORMATS
from .pipeline import EXIT_ERROR, Options, run
from .query import DATE_TYPES, FIELD_TAGS, QuerySpec

log = logging.getLogger("pubmed_fetch")

DEFAULTS: dict[str, Any] = {
    **{key: [] for key in FIELD_TAGS},
    "exclude": [],
    "query": None,
    "mesh_noexp": False,
    "days": 30,
    "start_date": None,
    "end_date": None,
    "date_type": None,
    "max_results": 1000,
    "pmids_file": None,
    "from_raw": None,
    "save_raw": None,
    "output_file": None,
    "format": None,
    "merge": False,
    "resume": False,
    "summary": None,
    "fulltext": True,
    "download_dir": None,
    "download_formats": ["pdf"],
    "icite": False,
    "since_last_run": None,
    "email": None,
    "api_key": None,
    "strict": False,
}

LIST_KEYS = (*FIELD_TAGS, "exclude", "download_formats")
INT_KEYS = ("days", "max_results")
DATE_KEYS = ("start_date", "end_date")
BOOL_KEYS = ("mesh_noexp", "fulltext", "icite", "merge", "resume", "strict")
STR_KEYS = tuple(k for k in DEFAULTS if k not in (*LIST_KEYS, *INT_KEYS, *DATE_KEYS, *BOOL_KEYS))
CHOICES = {"format": OUTPUT_FORMATS, "date_type": tuple(DATE_TYPES), "download_formats": DOWNLOAD_FORMATS}


def parse_list(value: str) -> list[str]:
    """
    Parse a config list: comma-separated and/or one item per line. Quote items that contain
    commas, e.g. ``"Carcinoma, Ductal, Breast", Breast Neoplasms``.
    """
    items: list[str] = []
    for line in value.splitlines():
        for row in csv.reader([line], skipinitialspace=True):
            items.extend(item.strip() for item in row if item.strip())
    return items


def _choice(value: str, key: str) -> str:
    value = value.strip().lower()
    if value not in CHOICES[key]:
        raise ValueError(f"{key} must be one of {', '.join(CHOICES[key])}, got {value!r}")
    return value


def load_config(config_file: str) -> dict[str, Any]:
    """
    Load settings from the [search] section of an INI file. Only keys present in the file are
    returned, so callers can layer CLI > environment > config > defaults.
    """
    config = configparser.ConfigParser()
    config.read(config_file)
    if "search" not in config:
        return {}
    section = config["search"]

    settings: dict[str, Any] = {}
    for key in section:
        if key not in DEFAULTS:
            log.warning("Ignoring unknown setting %r in %s", key, config_file)
    for key in LIST_KEYS:
        if key in section:
            settings[key] = parse_list(section[key])
    for key in INT_KEYS:
        if key in section:
            settings[key] = section.getint(key)
    for key in BOOL_KEYS:
        if section.get(key, "").strip():
            settings[key] = section.getboolean(key)
    for key in DATE_KEYS:
        if section.get(key, "").strip():
            settings[key] = date.fromisoformat(section[key].strip())
    for key in STR_KEYS:
        if section.get(key, "").strip():
            settings[key] = section[key].strip()
    for key in CHOICES:
        if isinstance(settings.get(key), str):
            settings[key] = _choice(settings[key], key)
    if "download_formats" in settings:
        settings["download_formats"] = [_choice(f, "download_formats") for f in settings["download_formats"]]
    return settings


def read_pmids(path: str) -> list[str]:
    """Read PMIDs separated by whitespace/commas from a file or stdin ('-'). Lines starting with # are ignored."""
    if path == "-":
        content = sys.stdin.read()
    else:
        with open(path, encoding="utf-8") as f:
            content = f.read()
    tokens = [
        t for line in content.splitlines() if not line.lstrip().startswith("#") for t in re.split(r"[\s,]+", line)
    ]
    tokens = [t for t in tokens if t]
    if bad := [t for t in tokens if not t.isdigit()]:
        raise ValueError(f"Invalid PMID(s) in {path}: {', '.join(bad[:5])}")
    return tokens


def _positive_int(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError(f"must be a positive integer, got {value}")
    return number


def _iso_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a date as YYYY-MM-DD, got {value!r}") from None


def _flag(group, *names: str, dest: str, const: bool = True, help: str) -> None:
    """A boolean flag that stays None when not given, so config file values can apply."""
    group.add_argument(*names, dest=dest, action="store_const", const=const, default=None, help=help)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pubmed-fetch",
        description="Search PubMed and export articles with abstracts, metadata and PMC full-text links.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s --mesh-terms "Endometrial Neoplasms" "Ovarian Neoplasms" --days 30
  %(prog)s --author "Smith J" --start-date 2024-01-01 --end-date 2024-12-31 --output smith.csv
  %(prog)s --query 'crispr[tiab] AND humans[mh]' --exclude 'Review[pt]' --output crispr.jsonl
  %(prog)s --mesh-terms "Breast Neoplasms" --since-last-run state.json --output breast.sqlite
  %(prog)s --pmids pmids.txt --icite --output - | jq .articles[].title
  %(prog)s --config config.ini

Exit codes: 0 success, 1 error, 2 partial results (some batches failed; see search_info.failed_batches).
Environment variables: NCBI_EMAIL, NCBI_API_KEY (used when --email / --api-key are not given).
        """,
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    filters = parser.add_argument_group("search filters (OR within a filter, AND across filters)")
    for flag, dest, metavar, help_text in (
        ("--mesh-terms", "mesh_terms", "TERM", "MeSH terms (includes narrower terms unless --mesh-noexp)"),
        ("--author", "authors", "NAME", 'Author names, e.g. "Smith J"'),
        ("--organization", "organizations", "TEXT", "Affiliation text"),
        ("--title-abstract", "title_abstract", "TEXT", "Words or phrases in the title or abstract"),
        ("--journal", "journals", "JOURNAL", 'Journal titles or abbreviations, e.g. "Nature"'),
        ("--publication-type", "publication_types", "TYPE", 'e.g. "Clinical Trial" "Review"'),
        ("--language", "languages", "LANGUAGE", "e.g. english"),
        ("--exclude", "exclude", "EXPR", 'PubMed expressions to exclude with NOT, e.g. "Review[pt]"'),
    ):
        # "extend": --author A --author B collects both, like --author A B.
        filters.add_argument(flag, dest=dest, metavar=metavar, nargs="+", action="extend", help=help_text)
    filters.add_argument("--query", metavar="QUERY", help="A raw PubMed query, ANDed with the other filters")
    _flag(filters, "--mesh-noexp", dest="mesh_noexp", help="Don't include narrower MeSH terms")

    dates = parser.add_argument_group("dates")
    when = dates.add_mutually_exclusive_group()
    when.add_argument(
        "--days", type=_positive_int, metavar="N", help="Days before the end date to include (default: 30)"
    )
    when.add_argument("--start-date", type=_iso_date, metavar="DATE", help="Earliest date (YYYY-MM-DD)")
    dates.add_argument("--end-date", type=_iso_date, metavar="DATE", help="Latest date (YYYY-MM-DD; default: today)")
    dates.add_argument(
        "--date-type",
        choices=DATE_TYPES,
        help="Which date to filter on: pdat publication (default), edat added to PubMed (best for monitoring), "
        "mdat modified",
    )
    dates.add_argument(
        "--max-results", type=_positive_int, metavar="N", help="Maximum number of articles to fetch (default: 1000)"
    )

    inputs = parser.add_argument_group("input (instead of a search)")
    inputs.add_argument(
        "--pmids", dest="pmids_file", metavar="FILE", help="Fetch the PMIDs listed in FILE ('-' = stdin)"
    )
    inputs.add_argument("--from-raw", metavar="DIR", help="Re-parse XML saved with --save-raw instead of fetching")
    inputs.add_argument("--save-raw", metavar="DIR", help="Also save the raw PubMed XML (gzipped) into DIR")

    output = parser.add_argument_group("output")
    output.add_argument(
        "--output",
        dest="output_file",
        metavar="FILE",
        help="Output file, or '-' for stdout (default: pubmed_articles.<format>); "
        "the extension (.json, .jsonl, .csv, .sqlite/.db) selects the format",
    )
    output.add_argument("--format", choices=OUTPUT_FORMATS, help="Output format (default: json)")
    _flag(output, "--merge", dest="merge", help="Update an existing output, replacing records with the same PMID")
    _flag(output, "--resume", dest="resume", help="Append to an existing output, skipping PMIDs already in it")
    output.add_argument("--summary", metavar="FILE", help="Write the run summary (search_info) as JSON ('-' = stdout)")

    enrich = parser.add_argument_group("full text and metrics")
    _flag(enrich, "--no-fulltext", dest="fulltext", const=False, help="Skip the PMC full-text availability check")
    enrich.add_argument(
        "--download-fulltext", dest="download_dir", metavar="DIR", help="Download available PMC full-text files"
    )
    enrich.add_argument(
        "--download-formats",
        nargs="+",
        action="extend",
        choices=DOWNLOAD_FORMATS,
        help="Formats to download (default: pdf)",
    )
    _flag(enrich, "--icite", dest="icite", help="Add NIH iCite citation metrics")

    other = parser.add_argument_group("incremental runs and NCBI access")
    other.add_argument(
        "--since-last-run",
        metavar="STATE",
        help="Continue from the end date stored in the STATE file and update it after a complete run "
        "(defaults --date-type to edat)",
    )
    other.add_argument("--email", metavar="EMAIL", help="Your email address for NCBI (recommended)")
    other.add_argument(
        "--api-key", metavar="KEY", help="NCBI API key; raises the rate limit from 3 to 10 requests/second"
    )
    _flag(other, "--strict", dest="strict", help="Fail if PubMed reports query errors or warnings")
    other.add_argument("--config", metavar="FILE", help="Configuration file (INI format)")
    other.add_argument("--create-config", metavar="FILENAME", help="Create a sample configuration file and exit")
    verbosity = other.add_mutually_exclusive_group()
    verbosity.add_argument("-q", "--quiet", action="store_true", help="Only print warnings and errors")
    verbosity.add_argument("-v", "--verbose", action="store_true", help="Print debug output")
    return parser


def parse_arguments(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments. Unset options are None so config values can apply."""
    return build_parser().parse_args(argv)


def resolve_settings(args: argparse.Namespace, config: dict[str, Any]) -> dict[str, Any]:
    """Merge settings with precedence: command line > environment > config file > defaults."""
    cli = {key: getattr(args, key, None) for key in DEFAULTS}
    env = {"email": os.environ.get("NCBI_EMAIL"), "api_key": os.environ.get("NCBI_API_KEY")}
    settings = dict(DEFAULTS)
    for layer in (config, env, cli):
        settings.update({k: v for k, v in layer.items() if v is not None})
    # --days on the command line replaces a start_date from the config file.
    if args.days is not None and args.start_date is None:
        settings["start_date"] = None
    return settings


def build_options(settings: dict[str, Any], quiet: bool = False) -> Options:
    spec = QuerySpec(
        **{key: settings[key] for key in FIELD_TAGS},
        raw_query=settings["query"],
        exclude=settings["exclude"],
        mesh_noexp=settings["mesh_noexp"],
    )
    names = (
        "days start_date end_date date_type max_results from_raw save_raw output_file format merge resume summary "
        "fulltext download_dir download_formats icite since_last_run email api_key strict"
    ).split()
    return Options(
        spec=spec,
        pmids=read_pmids(settings["pmids_file"]) if settings["pmids_file"] else None,
        display=not quiet,
        **{name: settings[name] for name in names},
    )


SAMPLE_CONFIG = """[search]
# Lists are comma-separated and/or one item per line. Quote items containing commas:
#   mesh_terms = "Carcinoma, Ductal, Breast", Breast Neoplasms
# Filters are ORed within a list and ANDed across lists.

# MeSH terms (narrower terms are included unless mesh_noexp = yes)
mesh_terms = Endometrial Neoplasms, Ovarian Neoplasms
#mesh_noexp = no

#authors = Gilbert P, Smith J
#organizations = Harvard Medical School, National Institutes of Health
#title_abstract = immunotherapy
#journals = Nature, "N Engl J Med"
#publication_types = Clinical Trial
#languages = english
# Raw PubMed query (ANDed with the filters above) and expressions to exclude with NOT
#query = humans[mh]
#exclude = Review[pt], Comment[pt]

# Date window: days before end_date (default today), or start_date..end_date (YYYY-MM-DD)
days = 30
#start_date = 2024-01-01
#end_date = 2024-12-31
# pdat = publication date, edat = date added to PubMed (best for monitoring), mdat = modified
#date_type = pdat

max_results = 1000

# Output file; the extension (.json, .jsonl, .csv, .sqlite) selects the format unless format is set
output_file = pubmed_articles.json
#format = jsonl
#merge = no
#resume = no
#summary = run_summary.json

# Full text and metrics
#fulltext = yes
#download_dir = papers
#download_formats = pdf, xml
#icite = no

# Incremental runs: continue from the end date stored in this file
#since_last_run = pubmed_state.json

# Save raw PubMed XML for re-parsing later with --from-raw
#save_raw = raw_xml

# Fail on PubMed query warnings such as unknown MeSH terms
#strict = no

# Your email address (recommended for NCBI API)
email = user@example.com

# NCBI API key (optional; or set NCBI_API_KEY)
#api_key =
"""


def create_sample_config(filename: str) -> bool:
    """Create a sample configuration file. Refuses to overwrite an existing file."""
    try:
        with open(filename, "x", encoding="utf-8") as f:
            f.write(SAMPLE_CONFIG)
    except OSError as e:
        log.error("Error creating config file: %s", e)
        return False
    log.info("Sample configuration file created: %s", filename)
    return True


def main(argv: list[str] | None = None) -> int:
    """Run the command line. Returns a process exit code."""
    args = parse_arguments(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING if args.quiet else logging.INFO,
        format="%(message)s",
        stream=sys.stderr,
    )

    if args.create_config:
        return 0 if create_sample_config(args.create_config) else EXIT_ERROR

    config: dict[str, Any] = {}
    if args.config:
        if not os.path.exists(args.config):
            log.error("Error: Configuration file '%s' not found.", args.config)
            return EXIT_ERROR
        try:
            config = load_config(args.config)
        except (configparser.Error, ValueError) as e:
            log.error("Error reading configuration file '%s': %s", args.config, e)
            return EXIT_ERROR

    try:
        settings = resolve_settings(args, config)
        options = build_options(settings, quiet=args.quiet)
        if not settings["email"] and options.mode != "raw":
            log.warning("Tip: pass --email (or set NCBI_EMAIL) so NCBI can contact you about heavy usage.")
        result = run(options)
    except (ValueError, PubMedError, OSError, sqlite3.Error) as e:
        log.error("Error: %s", e)
        return EXIT_ERROR

    info = result.search_info
    log.info("Done. Retrieved %d articles; %d in %s.", info["retrieved"], info["total_articles"], result.output)
    return result.exit_code


def main_cli() -> None:
    sys.exit(main())
