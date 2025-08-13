# PubMed Fetch

A simple CLI to search PubMed (NCBI E-utilities) for recent articles by MeSH terms and export structured JSON, including basic full-text link detection (PMC/DOI).

## Requirements
- Python 3.13 (see .python-version)
- uv package manager

## Install dependencies
```bash
uv sync
```

## Quick start
```bash
# Single MeSH term (last 30 days, custom output file)
uv run pubmed_fetch.py --mesh-terms "Breast Neoplasms" --days 30 --output breast-cancer-articles.json

# Multiple MeSH terms (space-separated)
uv run pubmed_fetch.py --mesh-terms "Endometrial Neoplasms" "Ovarian Neoplasms" --days 30 --output gyn-cancers.json

# Search by author (field-tagged as [Author])
uv run pubmed_fetch.py --author "Joe Smith" --days 30 --max-results 100

# Search by organization/affiliation (field-tagged as [Affiliation])
uv run pubmed_fetch.py --organization "FRED HUTCHINSON CANCER CENTER" --days 365

# Combine filters: AND across categories, OR within each category
uv run pubmed_fetch.py --mesh-terms "Breast Neoplasms" --author "Smith J" "Lee K" --organization "NIH" --days 90 --max-results 50 --email you@example.com
```

## Using a config file (INI)
An INI file can specify search parameters under a [search] section. See test_config.ini for an example.
```ini
[search]
# MeSH terms (optional; comma-separated)
mesh_terms = Endometrial Neoplasms, Ovarian Neoplasms

# Author filters (optional; comma-separated; e.g., Lastname Initials)
authors = Gilbert P, Smith J

# Organization/Affiliation filters (optional; comma-separated)
organizations = Harvard Medical School, National Institutes of Health

days = 30
max_results = 1000
output_file = pubmed_articles.json
email = you@example.com
```
Run with a config file:
```bash
uv run pubmed_fetch.py --config test_config.ini
```
Generate a starter config:
```bash
uv run pubmed_fetch.py --create-config my_config.ini
```

## Arguments (from --help)
- --mesh-terms: one or more MeSH terms (space-separated). Optional if using other filters.
- --author: one or more author names (space-separated), matched with the [Author] field.
- --organization: one or more organization/affiliation names (space-separated), matched with the [Affiliation] field.
- --days: past N days to include (default 30)
- --max-results: cap on PMIDs to fetch (default 1000)
- --output: output JSON filename (default pubmed_articles.json)
- --email: contact email for NCBI (recommended)
- --config: INI config path
- --create-config: write a sample INI to the given filename

Semantics: OR within a category (e.g., multiple authors), AND across categories (e.g., authors AND MeSH).

## Output
Writes JSON with search_info (terms, generated_on, date_range, total_articles) and an articles array including: pmid, title, authors, journal, date, abstract, MeSH terms, doi, pmc_id, url, and fulltext (if detected).

## Notes
- The tool respects NCBI rate limits with brief sleeps for batch requests and PMC OA checks.
- Provide a real email via --email or config for courteous API usage.

## Testing
Install dev dependencies (pytest, ruff):
```bash
uv sync --extra dev
```
Run all tests:
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
