# PubMed Fetch

A CLI and Python library that searches PubMed (NCBI E-utilities) and exports structured, schema-versioned records — abstracts, authors with affiliations and ORCIDs, MeSH terms with qualifiers, grants, retraction status, PubMed Central (PMC) full-text availability and optional citation metrics — as JSON, JSONL, CSV or SQLite, ready for downstream analysis.

## Requirements
- Python 3.10+ (development uses 3.13, see .python-version)
- uv package manager

## Install
```bash
uv sync
```
This installs the `pubmed-fetch` command: run it with `uv run pubmed-fetch ...` (or `uv run -m pubmed_fetch ...`). To install it globally: `uv tool install .`

## Quick start
```bash
# MeSH terms, last 30 days
uv run pubmed-fetch --mesh-terms "Endometrial Neoplasms" "Ovarian Neoplasms" --days 30 --output gyn.json

# Author in an absolute date range, as CSV (format inferred from the extension)
uv run pubmed-fetch --author "Smith J" --start-date 2024-01-01 --end-date 2024-12-31 --output smith-2024.csv

# Combine filters: OR within a filter, AND across filters
uv run pubmed-fetch --mesh-terms "Breast Neoplasms" --author "Smith J" "Lee K" --organization "NIH" --days 90

# Raw PubMed syntax, exclusions, journal and publication-type filters
uv run pubmed-fetch --query 'crispr[tiab] AND humans[mh]' --journal Nature Science --exclude 'Review[pt]' --output crispr.jsonl

# Weekly monitoring into a SQLite database (only articles added since the last run)
uv run pubmed-fetch --mesh-terms "Ovarian Neoplasms" --since-last-run ovarian.state.json --output ovarian.sqlite

# Fetch known PMIDs with citation metrics and pipe the JSON to another tool
uv run pubmed-fetch --pmids pmids.txt --icite --output - -q | jq '.articles[] | {pmid, title, rcr: .metrics.relative_citation_ratio}'

# Download open-access PDFs and XML
uv run pubmed-fetch --mesh-terms "Ovarian Neoplasms" --days 7 --download-fulltext papers --download-formats pdf xml
```

## Searching
| Option | PubMed field |
| --- | --- |
| `--mesh-terms` | `[MeSH Terms]` (includes narrower terms; `--mesh-noexp` for exact) |
| `--author` | `[Author]` |
| `--organization` | `[Affiliation]` |
| `--title-abstract` | `[Title/Abstract]` |
| `--journal` | `[Journal]` |
| `--publication-type` | `[Publication Type]` |
| `--language` | `[Language]` |
| `--query` | raw PubMed syntax, ANDed with the filters |
| `--exclude` | raw expressions removed with `NOT` |

Values are quoted, so multi-word values match as phrases. The query PubMed actually ran is recorded in `search_info.query_translation`.

**Typos are reported.** If PubMed doesn't recognize a term (e.g. a misspelled MeSH heading) or a field tag, a warning is printed and stored in `search_info.query_warnings`. With `--strict`, the run fails instead.

**Dates.** `--days N` (default 30) counts back from `--end-date` (default today); `--start-date` gives an absolute range. `--date-type` chooses which date is filtered:
- `pdat` (default): publication date. Note that articles can be indexed weeks after their publication date and some carry future issue dates.
- `edat`: the date the record was added to PubMed. Use this for monitoring "what's new".
- `mdat`: last modification date.

**Large result sets.** PubMed returns at most 10,000 records per search. When more match and `--max-results` allows, the date range is split automatically into windows that each fit, so all records can be retrieved (except on a single day with more than 10,000 matches, which is reported).

## Inputs other than a search
- `--pmids FILE`: fetch specific PMIDs (whitespace/comma separated, `#` comments allowed; `-` reads stdin).
- `--save-raw DIR`: also save the raw PubMed XML (gzipped) for every batch.
- `--from-raw DIR`: re-parse saved XML without contacting PubMed, e.g. after upgrading pubmed-fetch.

## Output
`--output FILE` (or `-` for stdout); the format comes from `--format` or the extension:

| Format | Extension | Notes |
| --- | --- | --- |
| `json` | `.json` | One document: `schema_version`, `search_info`, `articles`. Written atomically at the end. |
| `jsonl` | `.jsonl`, `.ndjson` | One article per line, written as batches arrive. |
| `csv` | `.csv` | One flattened row per article; lists joined with `; `. Written as batches arrive. |
| `sqlite` | `.sqlite`, `.db` | Normalized tables (below). Records are upserted by PMID, so repeated runs accumulate. |

Updating existing output:
- `--merge`: update an existing file, replacing records with the same PMID (json, jsonl, csv).
- `--resume`: continue an interrupted run, skipping PMIDs already in the output and appending the rest (jsonl, csv, sqlite, json).

`--summary FILE` writes the run's `search_info` as JSON (`-` for stdout), which is handy for orchestration tools.

Progress and diagnostics go to stderr. The human-readable listing goes to stdout unless `-q` is given or the data itself is written to stdout.

### Exit codes
| Code | Meaning |
| --- | --- |
| 0 | Success |
| 1 | Error (bad options, search failed, nothing could be fetched) |
| 2 | Partial results: some batches failed. Output is written with `search_info.complete = false` and details in `search_info.failed_batches`. `--since-last-run` state is not advanced, so the next run retries. |

### Record format (schema version 2)
The JSON Schema is in [`src/pubmed_fetch/schema/output.schema.json`](src/pubmed_fetch/schema/output.schema.json) (article records are `$defs/article`) and is available in Python via `pubmed_fetch.schema.load_schema()`. Missing values are `null`, never placeholder strings.

Each article includes:
- Identification: `pmid`, `record_type` (`journal_article` or `book`), `url`, `doi`, `pmc_id`, `article_ids` (all IDs PubMed lists for the record)
- Content: `title`, `abstract`, `abstract_sections` (`label`, `category`, `text`), `keywords`, `languages`, `publication_types`
- People: `authors` (`position`, `name`, `last_name`, `fore_name`, `initials`, `collective_name`, `orcid`, `affiliations`), `authors_display`
- Source: `journal`, `journal_abbrev`, `issn`, `issn_linking`, `volume`, `issue`, `pages`, `book_title`, `publisher`
- Dates: `date` (ISO, as precise as known: `2024`, `2024-02` or `2024-02-05`), `year`, `month`, `day`, `date_precision`, `date_raw` (seasons and ranges such as `1998 Winter`, `2023 Jan-Feb`), `epub_date`, `publication_status`
- Indexing: `mesh` (`descriptor`, `ui`, `major_topic`, `qualifiers`), `mesh_terms` (descriptor names), `grants` (`grant_id`, `acronym`, `agency`, `country`)
- Corrections: `comments_corrections` (`type` such as `RetractionIn` or `ErratumFor`, `pmid`, `source`), `is_retracted`
- `fulltext` (or `null`): `url`, `source` (`PMC`, `NCBI Bookshelf`, `Publisher (via DOI)`), `access_type`, `oa_checked`, `license`, `is_manuscript`, `retracted`, `download_links` (`format`, `url`, `path`)
- `metrics` (with `--icite`, else `null`): `citation_count`, `relative_citation_ratio`, `nih_percentile`, `field_citation_rate`, `expected_citations_per_year`, `citations_per_year`, `apt`, `is_clinical`, `is_research_article`. Very recent articles aren't in iCite yet and get `null`.

`fulltext.access_type`:
- `open`: in the PMC Open Access Subset; `license` and `download_links` come from the [PMC Cloud Service](https://pmc.ncbi.nlm.nih.gov/tools/pmcaws/)
- `free`: free to read (in PMC but not in the Open Access Subset, or NCBI Bookshelf). If `oa_checked` is false, the open-access lookup failed.
- `unknown`: not in PMC; a DOI link to the publisher is provided

`search_info` records provenance and completeness: `tool` (name, version), `generated_on` (UTC), `source` (`search`, `pmids` or `raw`), `query`, `query_translation`, `query_warnings`, `filters`, `date_type`, `date_range`, `date_windows`, `total_found`, `max_results`, `expected`, `retrieved`, `truncated`, `complete`, `failed_batches`, `skipped_records`, `skipped_existing`, lookup failure counts, and `total_articles`.

### SQLite tables
`articles` (one row per PMID, including `record_json` with the full record), `authors`, `affiliations`, `mesh` (one row per descriptor/qualifier pair), `keywords`, `publication_types`, `grants`, and `runs` (the `search_info` of every run). For example:
```sql
SELECT m.descriptor, COUNT(*) FROM mesh m JOIN articles a USING (pmid)
WHERE a.year >= 2024 AND m.descriptor_major GROUP BY 1 ORDER BY 2 DESC LIMIT 10;
```

## Incremental runs
`--since-last-run STATE` starts from the end date stored in the STATE file (or from `--days` on the first run), then updates the file after a complete run. It uses the Entrez date (`edat`) unless `--date-type` is given. Runs overlap by one day so nothing indexed late is missed; use SQLite output, `--merge` or `--resume` to de-duplicate.

## Using a config file (INI)
All options can go in a `[search]` section; see [sample_config.ini](sample_config.ini), or generate one with `uv run pubmed-fetch --create-config my_config.ini` (it won't overwrite an existing file). Lists are comma-separated and/or one per line; quote values that contain commas (common in MeSH terms):
```ini
[search]
mesh_terms = "Carcinoma, Ductal, Breast", Breast Neoplasms
journals =
    Nature
    Science
date_type = edat
icite = yes
output_file = results.sqlite
```
Unknown keys are reported, so typos don't go unnoticed. Precedence: command line > environment (`NCBI_EMAIL`, `NCBI_API_KEY`) > config file > defaults.

## Library use
```python
from pubmed_fetch import Options, PubMedClient, QuerySpec, run

# Full pipeline, same as the CLI
result = run(Options(spec=QuerySpec(mesh_terms=["Breast Neoplasms"]), days=7, output_file="out.jsonl", display=False))
print(result.exit_code, result.search_info["total_articles"])

# Records in memory, streamed in batches
client = PubMedClient(email="you@example.com")
for batch in client.iter_pmids(["28298962", "35187501"]):
    for article in batch:
        print(article["pmid"], article["title"])
```
The package doesn't configure logging; it logs to the `pubmed_fetch` logger.

## Notes
- Provide a real email via `--email` or `NCBI_EMAIL`, as NCBI requests. An [NCBI API key](https://support.nlm.nih.gov/kbArticle/?pn=KA-05317) raises the rate limit from 3 to 10 requests/second.
- Requests are rate limited and retried with backoff on 429/5xx errors; efetch batches are retried once more because NCBI's history server sometimes returns transient 400 errors.
- Downloaded files come from the PMC Article Datasets on AWS, which are licensed for reuse; check each article's `license` before redistributing.
- See [CHANGELOG.md](CHANGELOG.md) for changes between versions, including the 0.3.0 output-format changes.

## Development
```bash
uv sync --extra dev          # pytest, ruff, mypy, jsonschema
uv run -m pytest -q          # unit tests (offline, use real PubMed XML fixtures in tests/fixtures)
PUBMED_FETCH_LIVE=1 uv run -m pytest -q -m live   # contract tests against the real APIs
uv run ruff check . && uv run ruff format --check . && uv run mypy
```
GitHub Actions runs lint, type checks and tests on Python 3.10-3.13 for every push and pull request (`.github/workflows/ci.yml`), and the live API tests weekly (`.github/workflows/live.yml`; add an `NCBI_API_KEY` repository secret to use a key).
