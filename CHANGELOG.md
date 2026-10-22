# Changelog

## Unreleased

### Changed
- `--since-last-run` now filters on the modification date (`mdat`) instead of the Entrez date (`edat`) unless `--date-type` is given. PubMed adds MeSH terms weeks to months after an article is added, so `edat` monitoring of MeSH searches missed most articles (in a 7-day test, 29 by `edat` versus 205 by `mdat`). Runs now also refresh articles that changed. Pass `--date-type edat` for the old behavior; a state file written with `edat` triggers a warning saying so.

### Fixed
- Repeating a list option kept only its last value: `--author A --author B` searched for B only. Values now accumulate, as with `--author A B` (also for `--download-formats`).
- `--since-last-run` advanced its state even when `--max-results` cut the results, so later runs skipped the articles that weren't retrieved. The state now stays put, with a warning. (A single day over PubMed's 10,000-record limit still advances it, since retrying can't retrieve the excess.)
- The docs recommended `--resume` to de-duplicate incremental runs, which drops updated records; they now recommend SQLite output or `--merge`.

### Added
- `--max-results all` (and `max_results = all` in config files, `max_results=None` in the library) for no limit; `search_info.max_results` is then `null`.
- Agent skill for AI coding agents (Claude Code, Codex, Gemini CLI, GitHub Copilot, Cursor, Devin, …) in `skills/pubmed-fetch/`, following the Agent Skills standard. See "Using pubmed-fetch from AI coding agents" in the README.

## 0.3.0

### Breaking changes
- The code is now the `pubmed_fetch` package (in `src/`). Run it with `uv run pubmed-fetch` or `uv run -m pubmed_fetch`; `uv run pubmed_fetch.py` no longer works.
- Output schema version 2 (`schema_version: 2` in JSON output):
  - Missing values are `null` instead of `"N/A"` or `""`.
  - `authors` is a list of objects (`name`, `last_name`, `fore_name`, `initials`, `collective_name`, `orcid`, `affiliations`, `position`) instead of strings. `authors_display` is unchanged.
  - `search_info.mesh_terms`, `authors` and `organizations` moved into `search_info.filters`; `generated_on` is a UTC ISO-8601 timestamp.
  - `fulltext` always has the same keys (`license`, `is_manuscript`, `retracted`, `oa_checked`, `download_links` may be null/empty).
- An output file is now written even when nothing matches (with an empty `articles` list).
- Config lists are parsed as CSV: values containing commas must be quoted (`"Carcinoma, Ductal, Breast"`). Previously they were silently split.
- Requires Python 3.10+ (was 3.13+).

### Added
- More data per record: author affiliations and ORCIDs, journal abbreviation, ISSN, volume, issue, pages, electronic publication date, structured dates (`year`, `month`, `day`, `date_precision`, `date_raw`), languages, publication status, MeSH UIs, major-topic flags and qualifiers, grants, comments/corrections, `is_retracted`, all article IDs, structured abstract sections.
- NCBI Bookshelf records (`record_type: "book"`), which were previously dropped.
- PubMed query errors and warnings (e.g. unknown MeSH terms) are reported and recorded; `--strict` fails on them.
- `search_info` provenance and completeness: tool version, query translation, date type, expected/retrieved counts, `truncated`, `complete` and `failed_batches`. Exit code 2 for partial results.
- Filters: `--title-abstract`, `--journal`, `--publication-type`, `--language`, `--query` (raw PubMed syntax), `--exclude`, `--mesh-noexp`.
- `--date-type pdat|edat|mdat`.
- Automatic date-window splitting to retrieve more than PubMed's 10,000-record limit.
- `--since-last-run STATE` for incremental runs, `--merge`, `--resume`, `--pmids FILE`, `--save-raw DIR` / `--from-raw DIR`.
- SQLite output with normalized tables; `--output -` for stdout; `--summary FILE`.
- `--icite` citation metrics from NIH iCite.
- Published JSON Schema (`pubmed_fetch.schema`), a library API (`run`, `Options`, `PubMedClient`), type hints checked with mypy, offline tests on real PubMed XML, weekly live API tests in CI.

## 0.2.0
- Date ranges (`--start-date`/`--end-date`), CSV/JSONL output, open-access full-text downloads, GitHub Actions CI.
- Open-access detection moved to the PMC Cloud Service after NCBI retired the PMC OA Web Service.
- Fixes for IDs taken from cited references, abstracts truncated at inline markup and config precedence.
