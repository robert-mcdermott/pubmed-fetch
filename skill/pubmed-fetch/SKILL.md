---
name: pubmed-fetch
description: >-
  Search PubMed and collect biomedical literature with the pubmed-fetch CLI: find articles by MeSH term,
  author, affiliation, journal, publication type, keyword or raw PubMed query; fetch known PMIDs; get
  abstracts, authors/ORCIDs, MeSH, grants, retractions, open-access full text (PDF/XML/TXT) and iCite
  citation metrics; export JSON/JSONL/CSV/SQLite; monitor a topic over time. Use when the user asks
  about PubMed, MEDLINE, PMIDs, MeSH, biomedical/medical papers, literature searches or reviews,
  publication trends, citations, or building a literature dataset.
argument-hint: "[what to search for]"
allowed-tools:
  - read
  - grep
  - glob
permissions:
  allow:
    - Exec(uv run pubmed-fetch)
    - Exec(uvx --from git+https://github.com/robert-mcdermott/pubmed-fetch pubmed-fetch)
    - Exec(pubmed-fetch)
    - Exec(jq)
    - Exec(sqlite3)
---

# pubmed-fetch

`pubmed-fetch` searches PubMed (NCBI E-utilities) and writes structured, schema-versioned article records. Use it instead of scraping PubMed or calling E-utilities by hand: it handles rate limits, retries, PubMed's 10,000-record limit, open-access lookups and citation metrics.

Detailed field and database reference: [reference.md](reference.md).

## 1. Run the tool

Pick the first that works:

| Situation | Command prefix |
| --- | --- |
| Inside the pubmed-fetch repository | `uv run pubmed-fetch` |
| Installed (`uv tool install`) | `pubmed-fetch` |
| Anywhere else (no install needed) | `uvx --from git+https://github.com/robert-mcdermott/pubmed-fetch pubmed-fetch` |

Below, `pubmed-fetch` stands for whichever prefix applies. Check it works with `pubmed-fetch --version` (expect 0.3.0 or later).

**Always identify yourself to NCBI.** Use `--email` with the user's address if you know it, or rely on `NCBI_EMAIL` being set. Otherwise every run prints a "Tip: pass --email" warning, which is harmless. If `NCBI_API_KEY` is set, it's picked up automatically and raises the rate limit from 3 to 10 requests/second.

## 2. Workflow

1. **Translate the request into filters** (section 3). Prefer MeSH terms for topics, because they are curated and include narrower terms. Use `--title-abstract` for concepts too new or specific for MeSH.
2. **Probe the size first** when the scope is uncertain. It costs one request and returns how PubMed interpreted the query:
   ```bash
   pubmed-fetch --mesh-terms "Breast Neoplasms" --days 30 --max-results 1 --no-fulltext -q \
     --output /tmp/probe.json --summary - | jq '{query_translation, query_warnings, total_found}'
   ```
   - A non-empty `query_warnings` (e.g. `QuotedPhraseNotFound`) means a term wasn't recognized. Fix it before collecting; see [Finding the right MeSH term](#finding-the-right-mesh-term).
   - The "Only 1 of N matching articles will be retrieved" warning is expected for a probe.
   - If `total_found` is in the thousands, confirm the scope with the user or narrow it before fetching everything.
3. **Collect** into a file. Choose the format by purpose (section 4) and set `--max-results` deliberately (default 1000).
4. **Check completeness before using the data.** Exit code `0` means complete, `2` means partial (some batches failed; data is still written), `1` means error. `search_info.complete` and `search_info.failed_batches` say the same in the output.
5. **Read results with `jq` or `sqlite3`.** Never `cat` a large output file into the conversation. Extract only the fields you need (section 6).
6. **Report the query and its scope** when answering: the query translation, date range and date type, `total_found` versus retrieved, and whether results were capped by `--max-results`.

## 3. Building queries

Filters are ORed within one option and ANDed across options; values are quoted as phrases.

| User wants | Option |
| --- | --- |
| A topic (disease, drug, procedure) | `--mesh-terms "Ovarian Neoplasms"` (narrower terms included; `--mesh-noexp` for exact) |
| A word/phrase in title or abstract | `--title-abstract "long covid" "post-acute sequelae"` |
| An author | `--author "Smith J"` (last name + initials; full names like `"Smith John"` also work) |
| An institution | `--organization "Fred Hutchinson"` (substring of affiliation text) |
| A journal | `--journal "N Engl J Med" "Lancet"` (abbreviation or full title) |
| Study type | `--publication-type "Randomized Controlled Trial"` / `"Clinical Trial"` / `"Meta-Analysis"` / `"Systematic Review"` / `"Review"` |
| Language | `--language english` |
| Anything else in PubMed syntax | `--query 'humans[mh] AND 2020:2024[dp]'` (ANDed with the rest) |
| Exclude things | `--exclude 'Review[pt]' 'Case Reports[pt]' 'Comment[pt]'` |

**Dates.** The default is the last 30 days by publication date.
- `--days N` counts back from today (or from `--end-date`).
- `--start-date YYYY-MM-DD [--end-date YYYY-MM-DD]` gives an absolute range.
- `--date-type`:
  - `pdat` (default): publication date. It also matches electronic publication dates, so a 2025 range can include records whose issue date is 2026.
  - `edat`: when the record was added to PubMed. Use it for "what's new" and monitoring.
  - `mdat`: last modified.

**Inputs other than a search:** `--pmids FILE` (or `--pmids -` to read stdin) fetches known PMIDs; `--from-raw DIR` re-parses XML saved earlier with `--save-raw DIR`.

### Finding the right MeSH term
MeSH headings are formal ("Breast Neoplasms", not "breast cancer"; "Diabetes Mellitus, Type 2", not "type 2 diabetes"). If a guess produces a `query_warnings` entry, search free text and see which MeSH headings the matching articles carry:
```bash
pubmed-fetch --title-abstract "long covid" --days 60 --max-results 100 --no-fulltext -q --output - --format jsonl \
  | jq -r '.mesh[] | select(.major_topic or any(.qualifiers[]; .major_topic)) | .descriptor' \
  | sort | uniq -c | sort -rn | head
#   13 COVID-19
#    3 Post-Acute COVID-19 Syndrome
```
Then search with `--mesh-terms "Post-Acute COVID-19 Syndrome"`. Add `--strict` to make unrecognized terms a hard error (exit 1) in scripted runs.

## 4. Output formats

| Purpose | Use |
| --- | --- |
| You will read or filter the results | `--output results.jsonl` (one article per line; works with `jq`) |
| Analysis, joins, counts, repeated runs | `--output results.sqlite` (normalized tables; records upserted by PMID) |
| User wants a spreadsheet | `--output results.csv` |
| One self-contained document with provenance | `--output results.json` (`schema_version`, `search_info`, `articles`) |
| Pipe to another command | `--output - --format jsonl -q` |

The format comes from the file extension or `--format`. `--summary FILE` (or `-` for stdout) writes just the run metadata (`search_info`). `-q` hides the human-readable listing, which you rarely need.

Enrichment flags:
- `--icite` adds citation metrics (`citation_count`, `relative_citation_ratio`, `nih_percentile`). Articles from the last few months usually have none yet (`metrics: null`).
- `--no-fulltext` skips the PMC open-access check, which is faster when full text doesn't matter. It's on by default.
- `--download-fulltext DIR [--download-formats pdf xml txt]` downloads open-access files. PDFs average several MB each (about 350 MB for 60 articles in testing). Ask before large downloads, and prefer `txt` if the text is the goal.

## 5. Examples

**"What's been published on ovarian cancer immunotherapy in the last 3 months?"**

`--mesh-terms "Ovarian Neoplasms" "Immunotherapy"` would be wrong: it matches either term. To require both concepts, use two options:
```bash
pubmed-fetch --mesh-terms "Ovarian Neoplasms" --query '"Immunotherapy"[MeSH Terms]' --days 90 \
  --no-fulltext -q --output /tmp/ovca-io.jsonl --summary - | jq '{query_translation, total_found, complete}'
jq -r '[.date, .journal_abbrev, .title] | @tsv' /tmp/ovca-io.jsonl | head -20
```

**"Find randomized trials of X in 2025, English only, no reviews"**
```bash
pubmed-fetch --mesh-terms "Ovarian Neoplasms" --publication-type "Randomized Controlled Trial" --language english \
  --exclude 'Review[pt]' --start-date 2025-01-01 --end-date 2025-12-31 --no-fulltext -q --output /tmp/rct.jsonl
jq -c '{pmid, date, journal: .journal_abbrev, title}' /tmp/rct.jsonl
```

**"What has Dr. Silvia Morbelli at Genoa published, and how cited is it?"**
```bash
pubmed-fetch --author "Morbelli S" --organization "Genoa" --start-date 2015-01-01 --max-results 500 --icite \
  --no-fulltext -q --output /tmp/morbelli.sqlite --summary - | jq '{total_found, total_articles, complete}'
sqlite3 -header -column /tmp/morbelli.sqlite \
  "SELECT year, COUNT(*) AS papers, SUM(citation_count) AS citations FROM articles GROUP BY year ORDER BY year;"
```
Author names are ambiguous. Combining with `--organization` helps, and the ORCIDs in `authors` (or the `authors` table) can confirm identity.

**"Summarize these papers: 28298962, 35187501, 36452530"**
```bash
printf '28298962\n35187501\n36452530\n' | pubmed-fetch --pmids - --icite -q --output - --format jsonl \
  | jq '{pmid, title, date, journal: .journal_abbrev, is_retracted, cites: .metrics.citation_count, abstract}'
```
Always mention it if `is_retracted` is true.

**"Get the open-access full text so you can read the methods"**
```bash
pubmed-fetch --pmids - --download-fulltext /tmp/papers --download-formats txt -q --output /tmp/papers.jsonl <<< "28298962"
jq -r '.fulltext | .access_type, .license, (.download_links[] | select(.path) | .path)' /tmp/papers.jsonl
```
Then read the `.txt` file. `access_type` values:
- `open`: downloadable, licensed for reuse (check `license`).
- `free`: readable at `fulltext.url` but not downloadable.
- `unknown`: only a publisher DOI link.

**"Build a dataset of breast cancer papers from 2024 for analysis"** (large; PubMed's 10,000-record cap is handled by automatic date-window splitting)
```bash
pubmed-fetch --mesh-terms "Breast Neoplasms" --start-date 2024-01-01 --end-date 2024-12-31 --max-results 100000 \
  --icite --no-fulltext -q --output /tmp/breast-2024.sqlite --summary /tmp/breast-2024.summary.json
echo "exit: $?"; jq '{total_found, retrieved, complete, date_windows}' /tmp/breast-2024.summary.json
```
This takes minutes (roughly 200 records per request at 3 requests/second, faster with `NCBI_API_KEY`). Run it in the background or warn the user. If it's interrupted, rerun the same command with `--resume`.

**"Track new publications on this topic every week"**
```bash
pubmed-fetch --mesh-terms "Ovarian Neoplasms" --since-last-run ~/pubmed/ovarian.state.json \
  --output ~/pubmed/ovarian.sqlite -q --summary - | jq '{date_range, total_found, total_articles, complete}'
```
- The first run uses `--days` (default 30). Later runs start from the last run's end date, using `edat`.
- Runs overlap by one day on purpose. `total_found` counts this run's window and `total_articles` counts the whole database, so seeing the same few articles again on a same-day rerun is normal.
- The state only advances after a complete run.

## 6. Reading results efficiently

```bash
# Compact listing
jq -c '{pmid, date, journal: .journal_abbrev, title}' out.jsonl
# Count by year / journal / publication type
jq -r '.year' out.jsonl | sort | uniq -c
jq -r '.journal_abbrev' out.jsonl | sort | uniq -c | sort -rn | head
jq -r '.publication_types[]' out.jsonl | sort | uniq -c | sort -rn
# First and last authors with ORCIDs
jq -c '{pmid, first: .authors[0].name, last: .authors[-1].name, orcids: [.authors[].orcid | select(.)]}' out.jsonl
# Retracted articles
jq -c 'select(.is_retracted) | {pmid, title}' out.jsonl
# JSON documents (not JSONL): iterate .articles[]
jq -c '.articles[] | {pmid, title}' out.json
```
For SQLite, see the tables and example queries in [reference.md](reference.md). The `mesh` table has one row per qualifier, so use `COUNT(DISTINCT pmid)`.

## 7. Pitfalls

- **Values within one option are ORed.** To require two concepts, put them in different options or use `--query`.
- `--days` and `--start-date` can't be combined. `--since-last-run` can't be used with `--start-date` or `--pmids`.
- `--max-results` defaults to 1000. A warning reports how many matched versus how many were retrieved; mention it when it applies.
- Missing values are `null` (never "N/A"). `date` may be just a year (`date_precision`), and `date_raw` holds text such as "1998 Winter".
- `record_type: "book"` records (NCBI Bookshelf) have no journal and use `book_title`.
- An exit code of 2 means some data is missing. Say so, or rerun with `--resume` (append) or `--merge` (refresh).
- `--output -` can't be combined with SQLite, `--merge` or `--resume`.
- Don't run many searches in parallel: they share NCBI's per-user rate limit.
