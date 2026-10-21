# PubMed Fetch

A simple CLI to search PubMed (NCBI E-utilities) for recent articles by MeSH terms, authors and/or affiliations and export structured JSON with full abstracts and full-text availability from PubMed Central (PMC).

## Requirements
- Python 3.13 (see .python-version)
- uv package manager

## Install dependencies
```bash
uv sync
```
This also installs a `pubmed-fetch` command, so `uv run pubmed-fetch ...` and `uv run pubmed_fetch.py ...` are equivalent.

## Quick start
```bash
# Single MeSH term (last 30 days, custom output file)
uv run pubmed_fetch.py --mesh-terms "Breast Neoplasms" --days 30 --output breast-cancer-articles.json

# Multiple MeSH terms (space-separated)
uv run pubmed_fetch.py --mesh-terms "Endometrial Neoplasms" "Ovarian Neoplasms" --days 30 --output gyn-cancers.json

# Search by author (field-tagged as [Author])
uv run pubmed_fetch.py --author "Smith J" --days 30 --max-results 100

# Search by organization/affiliation (field-tagged as [Affiliation])
uv run pubmed_fetch.py --organization "FRED HUTCHINSON CANCER CENTER" --days 365

# Combine filters: AND across categories, OR within each category
uv run pubmed_fetch.py --mesh-terms "Breast Neoplasms" --author "Smith J" "Lee K" --organization "NIH" --days 90 --max-results 50 --email you@example.com

# Absolute date range, saved as CSV (format inferred from the extension)
uv run pubmed_fetch.py --author "Smith J" --start-date 2024-01-01 --end-date 2024-12-31 --output smith-2024.csv

# Download open-access PDFs and XML into ./papers
uv run pubmed_fetch.py --mesh-terms "Ovarian Neoplasms" --days 7 --download-fulltext papers --download-formats pdf xml
```

## Using a config file (INI)
An INI file can specify search parameters under a [search] section. See sample_config.ini for an example.
```ini
[search]
# MeSH terms (optional; comma-separated)
mesh_terms = Endometrial Neoplasms, Ovarian Neoplasms

# Author filters (optional; comma-separated; e.g., Lastname Initials)
authors = Sanders S, Smith J

# Organization/Affiliation filters (optional; comma-separated)
organizations = FRED HUTCHINSON CANCER CENTER

days = 30
# start_date = 2024-01-01
# end_date = 2024-12-31
max_results = 1000
output_file = pubmed_articles.json
# format = csv
# download_dir = papers
# download_formats = pdf, xml
email = you@example.com
# api_key = your-ncbi-api-key
```
Run with a config file:
```bash
uv run pubmed_fetch.py --config sample_config.ini
```
Generate a starter config (will not overwrite an existing file):
```bash
uv run pubmed_fetch.py --create-config my_config.ini
```

Precedence: command-line options > environment variables (`NCBI_EMAIL`, `NCBI_API_KEY`) > config file > defaults.

## Arguments (from --help)
- --mesh-terms: one or more MeSH terms (space-separated). Optional if using other filters.
- --author: one or more author names (space-separated), matched with the [Author] field.
- --organization: one or more organization/affiliation names (space-separated), matched with the [Affiliation] field.
- --days: number of days before the end date to include (default 30)
- --start-date / --end-date: absolute publication date range as YYYY-MM-DD (end defaults to today; `--start-date` can't be combined with `--days`)
- --max-results: cap on articles to fetch (default 1000; PubMed will not return more than 10,000)
- --output: output filename (default `pubmed_articles.<format>`); a `.json`, `.jsonl` or `.csv` extension selects the format
- --format: `json` (default), `jsonl` or `csv`; overrides the extension
- --email: contact email for NCBI (recommended; or `NCBI_EMAIL`)
- --api-key: NCBI API key, raises the rate limit from 3 to 10 requests/second (or `NCBI_API_KEY`)
- --no-fulltext: skip the PMC full-text availability check
- --download-fulltext DIR: download available PMC full-text files into DIR (existing files are skipped, so re-runs resume)
- --download-formats: any of `pdf`, `xml`, `txt` (default `pdf`)
- --config: INI config path
- --create-config: write a sample INI to the given filename
- -q/--quiet: only print warnings and errors (no result listing); -v/--verbose: debug output

Semantics: OR within a category (e.g., multiple authors), AND across categories (e.g., authors AND MeSH). Terms are quoted, so multi-word values are matched as phrases.

Progress and diagnostics go to stderr; the result listing goes to stdout. The exit code is non-zero if the search fails.

## Output
Formats:
- `json`: a single document with `search_info` and `articles` (described below)
- `jsonl`: one article object per line, convenient for streaming and `jq`
- `csv`: one row per article; list fields (authors, MeSH terms, keywords, publication types) are joined with `; `, and the full-text URL, access type and license are flattened into columns

JSON output contains:
- `search_info`: query, mesh_terms, authors, organizations, date_range, total_found (matches in PubMed), total_articles (retrieved), generated_on
- `articles`: pmid, title, authors, authors_display, journal, date (`YYYY-MM-DD` when available), abstract, mesh_terms, keywords, publication_types, doi, pmc_id, url, fulltext

`fulltext.access_type` is one of:
- `open`: in the PMC Open Access Subset; includes `license` and `download_links` (pdf/xml/txt) from the [PMC Cloud Service](https://pmc.ncbi.nlm.nih.gov/tools/pmcaws/)
- `free`: in PMC and free to read, but not in the Open Access Subset (author manuscripts may still have a `TDM` license and download links)
- `unknown`: not in PMC; a DOI link to the publisher is provided

With `--download-fulltext`, each downloaded entry in `download_links` also gets a `path` to the local file. Files come from the PMC Article Datasets on AWS, which are licensed for reuse; check each article's `license` before redistributing.

## Notes
- Requests are rate limited to NCBI's limits and retried with backoff on 429/5xx errors.
- PubMed searches use the E-utilities history server and fetch records in batches of 200.
- The PMC OA Web Service was retired by NCBI in August 2026; open-access status now comes from the PMC Article Datasets on AWS.

## Testing
GitHub Actions (`.github/workflows/ci.yml`) runs ruff lint, the ruff format check and pytest on pushes to `main`/`v2` and on pull requests.

Install dev dependencies (pytest, ruff):
```bash
uv sync --extra dev
```
Run all tests (they don't make network calls):
```bash
uv run -m pytest -q
```
Run a single test file:
```bash
uv run -m pytest tests/test_pubmed_fetch.py -q
```
Run a single test case:
```bash
uv run -m pytest tests/test_pubmed_fetch.py::test_build_query_and_params -q
```

## Linting and formatting (Ruff)
Run lint checks:
```bash
uv run ruff check .
```
Auto-fix simple issues:
```bash
uv run ruff check . --fix
```
Format code:
```bash
uv run ruff format .
```
