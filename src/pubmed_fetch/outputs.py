"""Output sinks (JSON, JSONL, CSV, SQLite), the human-readable listing and the run summary."""

from __future__ import annotations

import csv
import json
import logging
import os
import sqlite3
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import IO, Any

from ._meta import SCHEMA_VERSION, utc_now
from .parse import Article

log = logging.getLogger(__name__)

OUTPUT_FORMATS = ("json", "jsonl", "csv", "sqlite")
EXTENSIONS = {
    ".json": "json",
    ".jsonl": "jsonl",
    ".ndjson": "jsonl",
    ".csv": "csv",
    ".sqlite": "sqlite",
    ".db": "sqlite",
}
STDOUT = "-"

CSV_FIELDS = [
    "pmid",
    "record_type",
    "title",
    "authors",
    "first_author",
    "last_author",
    "orcids",
    "journal",
    "journal_abbrev",
    "issn",
    "volume",
    "issue",
    "pages",
    "date",
    "year",
    "month",
    "day",
    "date_precision",
    "epub_date",
    "publication_status",
    "doi",
    "pmc_id",
    "url",
    "languages",
    "publication_types",
    "mesh_terms",
    "mesh_major_topics",
    "keywords",
    "grant_agencies",
    "is_retracted",
    "fulltext_url",
    "access_type",
    "license",
    "citation_count",
    "relative_citation_ratio",
    "nih_percentile",
    "abstract",
]


def resolve_output(filename: str | None = None, fmt: str | None = None) -> tuple[str, str]:
    """Return (filename, format). The format comes from ``fmt``, else the file extension, else JSON."""
    ext_fmt = EXTENSIONS.get(Path(filename).suffix.lower()) if filename and filename != STDOUT else None
    fmt = fmt or ext_fmt or "json"
    if fmt not in OUTPUT_FORMATS:
        raise ValueError(f"Unknown output format: {fmt}")
    return filename or f"pubmed_articles.{'sqlite' if fmt == 'sqlite' else fmt}", fmt


def _join(values: Iterable[Any]) -> str:
    return "; ".join(str(v) for v in values if v not in (None, ""))


def csv_row(article: Article) -> dict[str, Any]:
    fulltext = article.get("fulltext") or {}
    metrics = article.get("metrics") or {}
    authors = article.get("authors") or []
    row = {key: article.get(key) for key in CSV_FIELDS}
    row.update(
        authors=_join(a["name"] for a in authors),
        first_author=authors[0]["name"] if authors else None,
        last_author=authors[-1]["name"] if authors else None,
        orcids=_join(a.get("orcid") for a in authors),
        languages=_join(article.get("languages") or []),
        publication_types=_join(article.get("publication_types") or []),
        mesh_terms=_join(article.get("mesh_terms") or []),
        mesh_major_topics=_join(
            m["descriptor"]
            for m in article.get("mesh") or []
            if m["major_topic"] or any(q["major_topic"] for q in m["qualifiers"])
        ),
        keywords=_join(article.get("keywords") or []),
        grant_agencies=_join(dict.fromkeys(g.get("agency") for g in article.get("grants") or [])),
        fulltext_url=fulltext.get("url"),
        access_type=fulltext.get("access_type"),
        license=fulltext.get("license"),
        citation_count=metrics.get("citation_count"),
        relative_citation_ratio=metrics.get("relative_citation_ratio"),
        nih_percentile=metrics.get("nih_percentile"),
    )
    return {k: "" if v is None else v for k, v in row.items()}


def _atomic_write(path: str, write) -> None:
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        write(f)
    os.replace(tmp, path)


class Sink:
    """
    Receives article batches as they are fetched. ``existing_pmids`` lists records already in
    the output that ``resume`` mode skips. ``close`` finalizes the output and returns the
    number of articles it contains.
    """

    streaming = False

    def __init__(self, path: str, merge: bool = False, resume: bool = False):
        self.path = path
        self.merge = merge
        self.resume = resume
        self.existing_pmids: set[str] = set()

    def write(self, batch: list[Article]) -> None:
        raise NotImplementedError

    def close(self, search_info: dict[str, Any]) -> int:
        raise NotImplementedError


class JsonSink(Sink):
    """A single JSON document. Held in memory and written atomically on close."""

    def __init__(self, path: str, merge: bool = False, resume: bool = False):
        super().__init__(path, merge, resume)
        self.articles: dict[str, Article] = {}
        if (merge or resume) and path != STDOUT and os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                doc = json.load(f)
            self.articles = {a["pmid"]: a for a in doc.get("articles", [])}
            if resume:
                self.existing_pmids = set(self.articles)

    def write(self, batch: list[Article]) -> None:
        self.articles.update((a["pmid"], a) for a in batch)

    def close(self, search_info: dict[str, Any]) -> int:
        doc = {
            "schema_version": SCHEMA_VERSION,
            "search_info": {**search_info, "total_articles": len(self.articles)},
            "articles": list(self.articles.values()),
        }
        if self.path == STDOUT:
            json.dump(doc, sys.stdout, indent=2, ensure_ascii=False)
            sys.stdout.write("\n")
        else:
            _atomic_write(self.path, lambda f: json.dump(doc, f, indent=2, ensure_ascii=False))
        return len(self.articles)


class _LineSink(Sink):
    """
    Base for line-oriented formats (JSONL, CSV). Batches are written and flushed as they arrive;
    ``resume`` appends to an existing file. ``merge`` rewrites the file at close, replacing
    existing records with the same PMID.
    """

    streaming = True

    def __init__(self, path: str, merge: bool = False, resume: bool = False):
        super().__init__(path, merge, resume)
        self._file: IO[str] | None = None
        self._count = 0
        self._previous: dict[str, Any] = {}
        exists = path != STDOUT and os.path.exists(path) and os.path.getsize(path) > 0
        if exists and resume:
            self._drop_partial_line()
        if exists and (merge or resume):
            self._previous = self._read_existing()
            if resume:
                self.existing_pmids = set(self._previous)
                self._count = len(self._previous)
        self._append = resume and exists

    def _drop_partial_line(self) -> None:
        """Remove a trailing partial line left by an interrupted run, so appended records stay valid."""
        with open(self.path, "rb+") as f:
            data = f.read()
            end = data.rfind(b"\n") + 1
            if end < len(data):
                log.warning("Removing an incomplete last line from %s (likely from an interrupted run)", self.path)
                f.truncate(end)

    def _read_existing(self) -> dict[str, Any]:
        raise NotImplementedError

    def _write_header(self, f: IO[str]) -> None:
        return None

    def _write_records(self, f: IO[str], batch: list[Article]) -> None:
        raise NotImplementedError

    def _write_previous(self, f: IO[str], previous: Iterable[Any]) -> None:
        raise NotImplementedError

    def _open(self) -> IO[str]:
        if self._file is None:
            if self.path == STDOUT:
                self._file = sys.stdout
            else:
                target = f"{self.path}.tmp" if self.merge else self.path
                self._file = open(target, "a" if self._append else "w", encoding="utf-8", newline="")
            if not self._append:
                self._write_header(self._file)
        return self._file

    def write(self, batch: list[Article]) -> None:
        f = self._open()
        self._write_records(f, batch)
        f.flush()
        self._count += len(batch)
        if self.merge:
            for article in batch:
                self._previous.pop(article["pmid"], None)

    def close(self, search_info: dict[str, Any]) -> int:
        f = self._open()
        if self.merge:
            self._write_previous(f, self._previous.values())
            self._count += len(self._previous)
        if f is not sys.stdout:
            f.close()
            if self.merge:
                os.replace(f"{self.path}.tmp", self.path)
        return self._count


class JsonlSink(_LineSink):
    """One article per line."""

    def _read_existing(self) -> dict[str, Any]:
        records = {}
        with open(self.path, encoding="utf-8") as f:
            for line in f:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    log.warning("Ignoring an incomplete line in %s (likely from an interrupted run)", self.path)
                    continue
                records[record["pmid"]] = record
        return records

    def _write_records(self, f: IO[str], batch: list[Article]) -> None:
        f.writelines(json.dumps(a, ensure_ascii=False) + "\n" for a in batch)

    def _write_previous(self, f: IO[str], previous: Iterable[Any]) -> None:
        f.writelines(json.dumps(a, ensure_ascii=False) + "\n" for a in previous)


class CsvSink(_LineSink):
    """One flattened row per article; list fields are joined with "; "."""

    def _read_existing(self) -> dict[str, Any]:
        with open(self.path, encoding="utf-8", newline="") as f:
            return {row["pmid"]: row for row in csv.DictReader(f) if row.get("pmid")}

    def _writer(self, f: IO[str]) -> csv.DictWriter:
        return csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")

    def _write_header(self, f: IO[str]) -> None:
        self._writer(f).writeheader()

    def _write_records(self, f: IO[str], batch: list[Article]) -> None:
        self._writer(f).writerows(map(csv_row, batch))

    def _write_previous(self, f: IO[str], previous: Iterable[Any]) -> None:
        self._writer(f).writerows({k: row.get(k, "") for k in CSV_FIELDS} for row in previous)


SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS articles (
    pmid TEXT PRIMARY KEY, record_type TEXT, title TEXT, abstract TEXT,
    journal TEXT, journal_abbrev TEXT, issn TEXT, volume TEXT, issue TEXT, pages TEXT,
    book_title TEXT, publisher TEXT,
    date TEXT, year INTEGER, month INTEGER, day INTEGER, date_precision TEXT, epub_date TEXT,
    publication_status TEXT, doi TEXT, pmc_id TEXT, url TEXT, is_retracted INTEGER,
    access_type TEXT, fulltext_url TEXT, license TEXT,
    citation_count INTEGER, relative_citation_ratio REAL, nih_percentile REAL,
    record_json TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS authors (
    pmid TEXT NOT NULL, position INTEGER NOT NULL, name TEXT, last_name TEXT, fore_name TEXT,
    initials TEXT, collective_name TEXT, orcid TEXT, PRIMARY KEY (pmid, position)
);
CREATE TABLE IF NOT EXISTS affiliations (pmid TEXT NOT NULL, author_position INTEGER NOT NULL, affiliation TEXT);
CREATE TABLE IF NOT EXISTS mesh (
    pmid TEXT NOT NULL, descriptor TEXT, descriptor_ui TEXT, descriptor_major INTEGER,
    qualifier TEXT, qualifier_ui TEXT, qualifier_major INTEGER
);
CREATE TABLE IF NOT EXISTS keywords (pmid TEXT NOT NULL, keyword TEXT);
CREATE TABLE IF NOT EXISTS publication_types (pmid TEXT NOT NULL, publication_type TEXT);
CREATE TABLE IF NOT EXISTS grants (pmid TEXT NOT NULL, grant_id TEXT, acronym TEXT, agency TEXT, country TEXT);
CREATE TABLE IF NOT EXISTS runs (run_id INTEGER PRIMARY KEY AUTOINCREMENT, finished_at TEXT, search_info TEXT);
CREATE INDEX IF NOT EXISTS idx_authors_orcid ON authors(orcid);
CREATE INDEX IF NOT EXISTS idx_affiliations_pmid ON affiliations(pmid);
CREATE INDEX IF NOT EXISTS idx_mesh_pmid ON mesh(pmid);
CREATE INDEX IF NOT EXISTS idx_mesh_descriptor ON mesh(descriptor);
CREATE INDEX IF NOT EXISTS idx_keywords_pmid ON keywords(pmid);
CREATE INDEX IF NOT EXISTS idx_publication_types_pmid ON publication_types(pmid);
CREATE INDEX IF NOT EXISTS idx_grants_pmid ON grants(pmid);
"""
SQLITE_CHILD_TABLES = ("authors", "affiliations", "mesh", "keywords", "publication_types", "grants")
SQLITE_ARTICLE_COLUMNS = [
    "pmid",
    "record_type",
    "title",
    "abstract",
    "journal",
    "journal_abbrev",
    "issn",
    "volume",
    "issue",
    "pages",
    "book_title",
    "publisher",
    "date",
    "year",
    "month",
    "day",
    "date_precision",
    "epub_date",
    "publication_status",
    "doi",
    "pmc_id",
    "url",
    "is_retracted",
]


class SqliteSink(Sink):
    """
    A normalized SQLite database. Records are always upserted by PMID, so repeated runs
    accumulate into the same database. Each batch is committed as it arrives.
    """

    streaming = True

    def __init__(self, path: str, merge: bool = False, resume: bool = False):
        super().__init__(path, merge, resume)
        self.db = sqlite3.connect(path)
        self.db.executescript(SQLITE_SCHEMA)
        if resume:
            self.existing_pmids = {row[0] for row in self.db.execute("SELECT pmid FROM articles")}

    def write(self, batch: list[Article]) -> None:
        now = utc_now()
        pmids = [(a["pmid"],) for a in batch]
        with self.db:
            for table in SQLITE_CHILD_TABLES:
                self.db.executemany(f"DELETE FROM {table} WHERE pmid = ?", pmids)
            columns = [*SQLITE_ARTICLE_COLUMNS, "access_type", "fulltext_url", "license"]
            columns += ["citation_count", "relative_citation_ratio", "nih_percentile", "record_json", "updated_at"]
            self.db.executemany(
                f"INSERT OR REPLACE INTO articles ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})",
                [self._article_row(a, now) for a in batch],
            )
            self.db.executemany(
                "INSERT INTO authors VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (a["pmid"], au["position"], au["name"], au["last_name"], au["fore_name"], au["initials"])
                    + (au["collective_name"], au["orcid"])
                    for a in batch
                    for au in a["authors"]
                ],
            )
            self.db.executemany(
                "INSERT INTO affiliations VALUES (?, ?, ?)",
                [(a["pmid"], au["position"], aff) for a in batch for au in a["authors"] for aff in au["affiliations"]],
            )
            self.db.executemany(
                "INSERT INTO mesh VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (a["pmid"], m["descriptor"], m["ui"], m["major_topic"], q["name"], q["ui"], q["major_topic"])
                    for a in batch
                    for m in a["mesh"]
                    for q in (m["qualifiers"] or [{"name": None, "ui": None, "major_topic": None}])
                ],
            )
            self.db.executemany(
                "INSERT INTO keywords VALUES (?, ?)", [(a["pmid"], k) for a in batch for k in a["keywords"]]
            )
            self.db.executemany(
                "INSERT INTO publication_types VALUES (?, ?)",
                [(a["pmid"], t) for a in batch for t in a["publication_types"]],
            )
            self.db.executemany(
                "INSERT INTO grants VALUES (?, ?, ?, ?, ?)",
                [
                    (a["pmid"], g["grant_id"], g["acronym"], g["agency"], g["country"])
                    for a in batch
                    for g in a["grants"]
                ],
            )

    @staticmethod
    def _article_row(article: Article, now: str) -> tuple:
        fulltext = article.get("fulltext") or {}
        metrics = article.get("metrics") or {}
        return (
            *(article.get(c) for c in SQLITE_ARTICLE_COLUMNS),
            fulltext.get("access_type"),
            fulltext.get("url"),
            fulltext.get("license"),
            metrics.get("citation_count"),
            metrics.get("relative_citation_ratio"),
            metrics.get("nih_percentile"),
            json.dumps(article, ensure_ascii=False),
            now,
        )

    def close(self, search_info: dict[str, Any]) -> int:
        with self.db:
            total = self.db.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
            self.db.execute(
                "INSERT INTO runs (finished_at, search_info) VALUES (?, ?)",
                (utc_now(), json.dumps({**search_info, "total_articles": total}, ensure_ascii=False)),
            )
        self.db.close()
        return total


SINKS: dict[str, type[Sink]] = {"json": JsonSink, "jsonl": JsonlSink, "csv": CsvSink, "sqlite": SqliteSink}


def open_sink(path: str, fmt: str, merge: bool = False, resume: bool = False) -> Sink:
    if path == STDOUT and (fmt == "sqlite" or merge or resume):
        raise ValueError("Writing to stdout ('-') can't be combined with sqlite, --merge or --resume.")
    if merge and resume:
        raise ValueError("--merge and --resume can't be used together.")
    return SINKS[fmt](path, merge=merge, resume=resume)


def save_results(articles: list[Article], search_info: dict[str, Any], filename=None, fmt=None, merge=False) -> int:
    """Write a complete list of articles in one go. Returns the number of articles in the output."""
    path, fmt = resolve_output(filename, fmt)
    sink = open_sink(path, fmt, merge=merge)
    sink.write(articles)
    return sink.close(search_info)


def write_summary(path: str, summary: dict[str, Any]) -> None:
    """Write the run summary (search_info) as JSON; ``-`` means stdout."""
    if path == STDOUT:
        json.dump(summary, sys.stdout, indent=2, ensure_ascii=False)
        sys.stdout.write("\n")
    else:
        _atomic_write(path, lambda f: json.dump(summary, f, indent=2, ensure_ascii=False))


def print_articles(articles: list[Article], start: int = 1, out: IO[str] | None = None) -> None:
    """Print a human-readable listing of articles."""
    out = out or sys.stdout
    for i, article in enumerate(articles, start):
        source = article.get("journal") or article.get("book_title")
        lines = [
            f"\n{i}. {article.get('title')}",
            f"   Authors: {article.get('authors_display')}",
            f"   {'Journal' if article.get('journal') else 'Book'}: {source}",
            f"   Date: {article.get('date')}" + (f" (epub {article['epub_date']})" if article.get("epub_date") else ""),
            f"   PMID: {article['pmid']}",
            f"   URL: {article['url']}",
        ]
        if article.get("is_retracted"):
            lines.append("   ** RETRACTED **")
        if article.get("mesh_terms"):
            suffix = "..." if len(article["mesh_terms"]) > 5 else ""
            lines.append(f"   MeSH Terms: {', '.join(article['mesh_terms'][:5])}{suffix}")
        if abstract := article.get("abstract"):
            lines.append(f"   Abstract: {abstract[:200] + '...' if len(abstract) > 200 else abstract}")
        if article.get("pmc_id"):
            lines.append(f"   PMC ID: {article['pmc_id']}")
        if fulltext := article.get("fulltext"):
            indicator = {
                "open": f" [Open Access{', ' + fulltext['license'] if fulltext.get('license') else ''}]",
                "free": " [Free in PMC]",
            }.get(fulltext["access_type"], "")
            lines.append(f"   Full-text: {fulltext['url']}{indicator}")
            if fulltext.get("download_links"):
                formats = ", ".join(link["format"] for link in fulltext["download_links"])
                lines.append(f"   Downloads: {formats} available")
        if metrics := article.get("metrics"):
            lines.append(
                f"   Citations: {metrics.get('citation_count')} (RCR {metrics.get('relative_citation_ratio')})"
            )
        lines.append("-" * 40)
        print("\n".join(lines), file=out)
