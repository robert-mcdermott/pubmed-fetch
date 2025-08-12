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

# Limit maximum results and set contact email (recommended by NCBI)
uv run pubmed_fetch.py --mesh-terms "COVID-19" --days 30 --max-results 50 --email you@example.com
```

## Using a config file (INI)
An INI file can specify search parameters under a [search] section. See test_config.ini for an example.
```ini
[search]
mesh_terms = Endometrial Neoplasms, Ovarian Neoplasms
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
- --mesh-terms: one or more MeSH terms (space-separated). Required unless using --config.
- --days: past N days to include (default 30)
- --max-results: cap on PMIDs to fetch (default 1000)
- --output: output JSON filename (default pubmed_articles.json)
- --email: contact email for NCBI (recommended)
- --config: INI config path
- --create-config: write a sample INI to the given filename

## Output
Writes JSON with search_info (terms, generated_on, date_range, total_articles) and an articles array including: pmid, title, authors, journal, date, abstract, MeSH terms, doi, pmc_id, url, and fulltext (if detected).

## Notes
- The tool respects NCBI rate limits with brief sleeps for batch requests and PMC OA checks.
- Provide a real email via --email or config for courteous API usage.
