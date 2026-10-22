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

Values are quoted, so multi-word values match as phrases. Repeating an option adds to it: `--author A --author B` is the same as `--author A B`. The query PubMed actually ran is recorded in `search_info.query_translation`.

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
| Table | One row per | Key columns |
| --- | --- | --- |
| `articles` | article (primary key `pmid`) | `title`, `abstract`, `journal`, `journal_abbrev`, `date`, `year`, `epub_date`, `doi`, `pmc_id`, `is_retracted`, `access_type`, `fulltext_url`, `license`, `citation_count`, `relative_citation_ratio`, `nih_percentile`, `record_json` (the full record) |
| `authors` | author of an article | `pmid`, `position`, `name`, `last_name`, `fore_name`, `orcid` |
| `affiliations` | author affiliation | `pmid`, `author_position`, `affiliation` |
| `mesh` | MeSH descriptor/qualifier pair | `pmid`, `descriptor`, `descriptor_major`, `qualifier`, `qualifier_major` |
| `keywords`, `publication_types`, `grants` | keyword / type / grant | `pmid`, plus `keyword` / `publication_type` / `grant_id`, `agency`, `country` |
| `runs` | run of pubmed-fetch | `finished_at`, `search_info` (JSON) |

The `mesh` table has one row per qualifier, so use `COUNT(DISTINCT pmid)` when counting articles.

### Example: build and query a research database
Collect a quarter's worth of ovarian cancer articles, with citation metrics and the open-access PDFs:
```bash
uv run pubmed-fetch --mesh-terms "Ovarian Neoplasms" --start-date 2024-01-01 --end-date 2024-03-31 \
  --max-results 60 --icite --download-fulltext papers --output ovarian.sqlite --email you@example.com
```
Open-access PDFs average several MB each (60 articles downloaded about 350 MB in testing), so start with a small `--max-results` or add `--download-formats txt` for plain text only. Running more searches with the same `--output` adds to the same database.

Then query it with the `sqlite3` command-line tool (preinstalled on macOS and most Linux systems), e.g. `sqlite3 -header -column ovarian.sqlite "<query>"`:

```sql
-- Overview
SELECT COUNT(*) AS articles, SUM(access_type = 'open') AS open_access,
       SUM(is_retracted) AS retracted, ROUND(AVG(citation_count), 1) AS avg_citations
FROM articles;

-- Most-cited articles (needs --icite)
SELECT pmid, citation_count AS cites, ROUND(relative_citation_ratio, 2) AS rcr, title
FROM articles WHERE citation_count IS NOT NULL
ORDER BY citation_count DESC LIMIT 10;

-- Main topics: MeSH terms marked as a major topic (directly or via a qualifier)
SELECT descriptor, COUNT(DISTINCT pmid) AS articles
FROM mesh WHERE descriptor_major OR qualifier_major
GROUP BY descriptor ORDER BY articles DESC LIMIT 10;

-- Journals, and how much of each is open access
SELECT journal_abbrev AS journal, COUNT(*) AS articles, SUM(access_type = 'open') AS open_access
FROM articles GROUP BY journal_abbrev ORDER BY articles DESC LIMIT 10;

-- Funders
SELECT agency, COUNT(DISTINCT pmid) AS articles
FROM grants WHERE agency IS NOT NULL
GROUP BY agency ORDER BY articles DESC LIMIT 10;

-- Most frequent authors, identified by ORCID rather than by name
SELECT name, orcid, COUNT(*) AS articles
FROM authors WHERE orcid IS NOT NULL
GROUP BY orcid ORDER BY articles DESC LIMIT 10;

-- Articles with an author at a given institution
SELECT DISTINCT a.pmid, a.title
FROM articles a JOIN affiliations f USING (pmid)
WHERE f.affiliation LIKE '%Fudan University%';

-- Abstracts mentioning a phrase, newest first
SELECT pmid, date, title FROM articles
WHERE abstract LIKE '%PARP inhibitor%' ORDER BY date DESC;

-- Retracted articles and their retraction notices
SELECT pmid, title, json_extract(c.value, '$.pmid') AS retraction_notice
FROM articles, json_each(record_json, '$.comments_corrections') AS c
WHERE is_retracted AND json_extract(c.value, '$.type') = 'RetractionIn';

-- Downloaded files, with their licenses (paths are relative to where you ran pubmed-fetch)
SELECT a.pmid, a.license, json_extract(link.value, '$.path') AS file
FROM articles a, json_each(a.record_json, '$.fulltext.download_links') AS link
WHERE json_extract(link.value, '$.path') IS NOT NULL;

-- History of the runs that built the database
SELECT run_id, finished_at,
       json_extract(search_info, '$.date_range.start_date') AS start_date,
       json_extract(search_info, '$.date_range.end_date') AS end_date,
       json_extract(search_info, '$.total_found') AS found,
       json_extract(search_info, '$.total_articles') AS in_database
FROM runs;
```
Fields that aren't columns can be read from `record_json` with SQLite's JSON functions, as in the last three queries.

The same database loads straight into pandas (here run ad hoc with `uv run --with pandas`, without adding it to the project):
```bash
uv run --with pandas python - <<'EOF'
import sqlite3
import pandas as pd

db = sqlite3.connect("ovarian.sqlite")
articles = pd.read_sql("SELECT pmid, year, journal_abbrev, access_type, citation_count FROM articles", db)
print(articles.groupby("access_type")["citation_count"].describe()[["count", "mean", "max"]])

topics = pd.read_sql("SELECT DISTINCT pmid, descriptor FROM mesh WHERE descriptor_major OR qualifier_major", db)
print(topics["descriptor"].value_counts().head(10))
EOF
```

To keep the database current, run the same search on a schedule with `--since-last-run` (see [Incremental runs](#incremental-runs)); new articles are added and existing ones refreshed.

## Incremental runs
`--since-last-run STATE` starts from the end date stored in the STATE file (or from `--days` on the first run), then updates the file after a complete run. The file isn't updated when `--max-results` cut the results, since that would skip the articles that weren't retrieved. It uses the Entrez date (`edat`) unless `--date-type` is given. Runs overlap by one day so nothing indexed late is missed; use SQLite output, `--merge` or `--resume` to de-duplicate.

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

## Using pubmed-fetch from AI coding agents (agent skill)
[`skills/pubmed-fetch/`](skills/pubmed-fetch/) is an agent skill in the open [Agent Skills](https://agentskills.io) format, which Claude Code, OpenAI Codex, Gemini CLI, GitHub Copilot, Cursor, Devin and other coding agents support. It teaches an agent to use this tool well:
- run it, with no install needed
- check the result count and catch misspelled MeSH terms before collecting
- turn requests into the right filters
- pick an output format and confirm the results are complete
- read results with `jq`/`sqlite3` instead of loading whole files

`SKILL.md` holds the workflow and worked examples; `reference.md` holds the record fields, SQLite tables and example queries, and is loaded only when needed.

**Requirements:** the agent must be able to run shell commands, with [uv](https://docs.astral.sh/uv/) installed and network access to NCBI. The skill runs the tool with `uvx --from git+https://github.com/robert-mcdermott/pubmed-fetch pubmed-fetch` when it isn't installed, so users don't need to install pubmed-fetch first. `jq` and `sqlite3` are used for reading results.

### Install with a skills installer
Either of these copies the skill into the right folder for the agents you choose, at project or personal scope:
```bash
# skills CLI (Node.js 18+); supports Claude Code, Codex, Cursor, Copilot, Gemini CLI and more
npx skills add robert-mcdermott/pubmed-fetch

# GitHub CLI (skill support is in preview); see `gh skill install --help` for --agent and --scope
gh skill install robert-mcdermott/pubmed-fetch pubmed-fetch
```

### Install manually
Copy (or symlink) the `skills/pubmed-fetch` folder into a skills folder your agent reads:

| Agent | Project (commit it to share with a team) | Personal (all your projects) |
| --- | --- | --- |
| Claude Code | `.claude/skills/` | `~/.claude/skills/` |
| OpenAI Codex | `.agents/skills/` | `~/.agents/skills/` |
| Gemini CLI | `.agents/skills/` or `.gemini/skills/` | `~/.agents/skills/` or `~/.gemini/skills/` |
| GitHub Copilot | `.github/skills/`, `.agents/skills/` or `.claude/skills/` | `~/.agents/skills/` or `~/.copilot/skills/` |
| Devin | `.agents/skills/` or `.devin/skills/` | `~/.agents/skills/` or `~/.config/devin/skills/` |
| Cursor, Cline, OpenCode, Amp, Warp and others | `.agents/skills/` | see the agent's documentation |

`~/.agents/skills/` is the shared location: one copy there covers every agent above except Claude Code. For example:
```bash
git clone https://github.com/robert-mcdermott/pubmed-fetch.git
mkdir -p ~/.agents/skills ~/.claude/skills
cp -r pubmed-fetch/skills/pubmed-fetch ~/.agents/skills/   # Codex, Gemini CLI, Copilot, Devin, Cursor, ...
cp -r pubmed-fetch/skills/pubmed-fetch ~/.claude/skills/   # Claude Code
```
To pick up updates with `git pull`, use a symlink instead of a copy, e.g. `ln -s "$PWD/pubmed-fetch/skills/pubmed-fetch" ~/.agents/skills/pubmed-fetch`. Codex documents symlink support; if your agent doesn't follow symlinks, copy the folder instead. Gemini CLI loads project-level skills only in folders you've marked as trusted (`/trust`). Restart the agent, or reload its skills, after installing.

### Use it
Ask in plain language, and the agent loads the skill when the request matches its description:
> Find randomized controlled trials on ovarian cancer published in 2025, excluding reviews, and summarize the most cited ones.

> Build a SQLite database of breast cancer papers from 2024 with citation metrics, then show the top journals and funders.

> Here are three PMIDs: 28298962, 35187501, 36452530. Summarize them and flag any retractions.

You can also invoke it by name: `/pubmed-fetch` in Claude Code and Devin, or `$pubmed-fetch` in Codex.

Agents ask before running shell commands. To skip those prompts, allow commands that start with `uvx --from git+https://github.com/robert-mcdermott/pubmed-fetch pubmed-fetch` (or `pubmed-fetch` / `uv run pubmed-fetch`), `jq` and `sqlite3` in your agent's permission settings.

For an agent without skill support, point it at the file instead, e.g. "Read `skills/pubmed-fetch/SKILL.md` and follow it to search PubMed for …", or paste the contents into its instructions file (such as `AGENTS.md`).

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
When CLI options or the output change, update the agent skill in `skills/pubmed-fetch/` to match.

GitHub Actions runs lint, type checks and tests on Python 3.10-3.13 for every push and pull request (`.github/workflows/ci.yml`), and the live API tests weekly (`.github/workflows/live.yml`; add an `NCBI_API_KEY` repository secret to use a key).
