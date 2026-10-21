# pubmed-fetch reference

Output schema version 2. The authoritative JSON Schema is `src/pubmed_fetch/schema/output.schema.json` in the pubmed-fetch repository (article records are `$defs/article`).

## Article record

| Field | Type | Notes |
| --- | --- | --- |
| `pmid` | string | PubMed ID |
| `record_type` | `journal_article` \| `book` | Books are NCBI Bookshelf records |
| `url` | string | PubMed page |
| `title`, `abstract` | string \| null | Abstract sections are joined as `LABEL: text` |
| `abstract_sections` | list of `{label, category, text}` | Structured abstracts (BACKGROUND, METHODS, …) |
| `authors` | list of `{position, name, last_name, fore_name, initials, collective_name, orcid, affiliations[]}` | `name` is "Last, Fore" or a group name |
| `authors_display` | string | First 5 names, then "et al." |
| `journal`, `journal_abbrev`, `issn`, `issn_linking`, `volume`, `issue`, `pages` | string \| null | |
| `book_title`, `publisher` | string \| null | Books only |
| `date` | string \| null | `YYYY`, `YYYY-MM` or `YYYY-MM-DD` (issue date) |
| `year`, `month`, `day` | int \| null | |
| `date_precision` | `day` \| `month` \| `year` \| null | |
| `date_raw` | string \| null | Seasons and ranges, e.g. "1998 Winter", "2023 Jan-Feb" |
| `epub_date` | string \| null | Electronic publication date (books: contribution date) |
| `languages`, `publication_types`, `keywords`, `mesh_terms` | list of strings | `mesh_terms` = descriptor names |
| `publication_status` | string \| null | e.g. `ppublish`, `epublish` |
| `mesh` | list of `{descriptor, ui, major_topic, qualifiers: [{name, ui, major_topic}]}` | |
| `grants` | list of `{grant_id, acronym, agency, country}` | |
| `doi`, `pmc_id` | string \| null | |
| `article_ids` | object | All IDs (pubmed, pmc, doi, pii, mid, bookaccession, …) |
| `comments_corrections` | list of `{type, pmid, source}` | `type` e.g. `RetractionIn`, `ErratumIn`, `CommentIn` |
| `is_retracted` | bool | |
| `fulltext` | object \| null | See below |
| `metrics` | object \| null | With `--icite`; null if iCite has no record yet |

`fulltext`: `url`, `source` (`PMC`, `NCBI Bookshelf`, `Publisher (via DOI)`), `access_type` (`open`, `free`, `unknown`), `oa_checked` (false if the open-access lookup failed), `license` (e.g. `CC BY`), `is_manuscript`, `retracted`, `download_links` (list of `{format, url, path}`; `path` is set after `--download-fulltext`).

`metrics`: `source` (`icite`), `citation_count`, `relative_citation_ratio` (1.0 = field average for NIH-funded papers), `nih_percentile`, `field_citation_rate`, `expected_citations_per_year`, `citations_per_year`, `apt` (approximate potential to translate), `is_clinical`, `is_research_article`.

## search_info (run metadata)

`tool` (name, version), `generated_on` (UTC), `source` (`search`, `pmids` or `raw`), `query`, `query_translation` (what PubMed actually ran), `query_warnings`, `filters`, `date_type`, `date_range`, `date_windows`, `total_found` (matches in PubMed), `max_results`, `expected`, `retrieved`, `truncated` (results were capped), `complete` (no batch failed), `failed_batches`, `skipped_records`, `skipped_existing` (`--resume`), `fulltext_lookup_failures`, `icite_lookup_failures`, `downloads`, `total_articles` (articles in the output file or database).

## SQLite tables

| Table | One row per | Columns |
| --- | --- | --- |
| `articles` | article (PK `pmid`) | `record_type`, `title`, `abstract`, `journal`, `journal_abbrev`, `issn`, `volume`, `issue`, `pages`, `book_title`, `publisher`, `date`, `year`, `month`, `day`, `date_precision`, `epub_date`, `publication_status`, `doi`, `pmc_id`, `url`, `is_retracted`, `access_type`, `fulltext_url`, `license`, `citation_count`, `relative_citation_ratio`, `nih_percentile`, `record_json`, `updated_at` |
| `authors` | author | `pmid`, `position`, `name`, `last_name`, `fore_name`, `initials`, `collective_name`, `orcid` |
| `affiliations` | author affiliation | `pmid`, `author_position`, `affiliation` |
| `mesh` | descriptor/qualifier pair | `pmid`, `descriptor`, `descriptor_ui`, `descriptor_major`, `qualifier`, `qualifier_ui`, `qualifier_major` |
| `keywords` | keyword | `pmid`, `keyword` |
| `publication_types` | type | `pmid`, `publication_type` |
| `grants` | grant | `pmid`, `grant_id`, `acronym`, `agency`, `country` |
| `runs` | run | `run_id`, `finished_at`, `search_info` (JSON) |

Anything not in a column is in `articles.record_json`; read it with `json_extract` / `json_each`.

```sql
-- Overview
SELECT COUNT(*) AS articles, SUM(access_type = 'open') AS open_access,
       SUM(is_retracted) AS retracted, ROUND(AVG(citation_count), 1) AS avg_citations
FROM articles;

-- Publications per year
SELECT year, COUNT(*) AS articles FROM articles GROUP BY year ORDER BY year;

-- Most cited (needs --icite)
SELECT pmid, citation_count, ROUND(relative_citation_ratio, 2) AS rcr, title
FROM articles WHERE citation_count IS NOT NULL ORDER BY citation_count DESC LIMIT 10;

-- Main topics (major MeSH headings)
SELECT descriptor, COUNT(DISTINCT pmid) AS articles
FROM mesh WHERE descriptor_major OR qualifier_major
GROUP BY descriptor ORDER BY articles DESC LIMIT 10;

-- Top journals and their open-access share
SELECT journal_abbrev, COUNT(*) AS articles, SUM(access_type = 'open') AS open_access
FROM articles GROUP BY journal_abbrev ORDER BY articles DESC LIMIT 10;

-- Funders
SELECT agency, COUNT(DISTINCT pmid) AS articles FROM grants
WHERE agency IS NOT NULL GROUP BY agency ORDER BY articles DESC LIMIT 10;

-- Prolific authors, by ORCID
SELECT name, orcid, COUNT(*) AS articles FROM authors
WHERE orcid IS NOT NULL GROUP BY orcid ORDER BY articles DESC LIMIT 10;

-- Articles from an institution
SELECT DISTINCT a.pmid, a.title FROM articles a JOIN affiliations f USING (pmid)
WHERE f.affiliation LIKE '%Fudan University%';

-- Phrase in abstracts
SELECT pmid, date, title FROM articles WHERE abstract LIKE '%PARP inhibitor%' ORDER BY date DESC;

-- Retracted articles and their retraction notices
SELECT pmid, title, json_extract(c.value, '$.pmid') AS retraction_notice
FROM articles, json_each(record_json, '$.comments_corrections') AS c
WHERE is_retracted AND json_extract(c.value, '$.type') = 'RetractionIn';

-- Downloaded files
SELECT a.pmid, a.license, json_extract(link.value, '$.path') AS file
FROM articles a, json_each(a.record_json, '$.fulltext.download_links') AS link
WHERE json_extract(link.value, '$.path') IS NOT NULL;

-- Run history
SELECT run_id, finished_at, json_extract(search_info, '$.total_found') AS found,
       json_extract(search_info, '$.total_articles') AS in_database
FROM runs;
```

## Exit codes

| Code | Meaning | What to do |
| --- | --- | --- |
| 0 | Complete | |
| 1 | Error: bad options, search failed, nothing fetched, or a `--strict` query warning | Read the stderr message |
| 2 | Partial: some batches failed; output written with `complete: false` | Report it; rerun with `--resume` or `--merge` |

## All options

Run `pubmed-fetch --help` for the authoritative list. Every option can also be set in an INI config file (`--config FILE`, `[search]` section; `--create-config FILE` writes a commented template). Precedence: command line > `NCBI_EMAIL` / `NCBI_API_KEY` environment variables > config file > defaults.
