"""Run a complete fetch: search or load records, enrich them, and stream them to an output."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import requests

from ._meta import SCHEMA_VERSION, TOOL_NAME, PubMedError, __version__, utc_now
from .client import PubMedClient
from .fulltext import DOWNLOAD_FORMATS, PMCFullText
from .icite import add_icite_metrics
from .outputs import STDOUT, Sink, open_sink, print_articles, resolve_output, write_summary
from .parse import Article
from .query import DATE_TYPES, QuerySpec, resolve_date_range
from .state import load_state, save_state, start_from_state

log = logging.getLogger(__name__)

EXIT_OK, EXIT_ERROR, EXIT_PARTIAL = 0, 1, 2


@dataclass
class Options:
    """Everything that controls a run. See ``pubmed-fetch --help`` for descriptions."""

    spec: QuerySpec = field(default_factory=QuerySpec)
    days: int = 30
    start_date: date | None = None
    end_date: date | None = None
    date_type: str | None = None  # default: pdat, or edat with since_last_run
    max_results: int = 1000
    pmids: list[str] | None = None
    from_raw: str | None = None
    save_raw: str | None = None
    output_file: str | None = None
    format: str | None = None
    merge: bool = False
    resume: bool = False
    summary: str | None = None
    fulltext: bool = True
    download_dir: str | None = None
    download_formats: list[str] = field(default_factory=lambda: ["pdf"])
    icite: bool = False
    since_last_run: str | None = None
    email: str | None = None
    api_key: str | None = None
    strict: bool = False
    display: bool = True

    @property
    def mode(self) -> str:
        return "pmids" if self.pmids is not None else "raw" if self.from_raw else "search"

    def resolved_date_type(self) -> str:
        return self.date_type or ("edat" if self.since_last_run else "pdat")

    def validate(self) -> None:
        """Raise ValueError for invalid or conflicting options (before any network access)."""
        modes = [not self.spec.is_empty(), self.pmids is not None, bool(self.from_raw)]
        if not any(modes):
            raise ValueError(
                "No search filters specified. Use --mesh-terms, --author, --organization, --query (or another "
                "filter), --pmids, --from-raw, or provide a config file."
            )
        if sum(modes) > 1:
            raise ValueError("Use only one of: search filters, --pmids, or --from-raw.")
        if self.since_last_run and (self.mode != "search" or self.start_date):
            raise ValueError("--since-last-run works with searches only and can't be combined with --start-date.")
        if self.download_dir and not self.fulltext:
            raise ValueError("--download-fulltext needs the full-text check; remove --no-fulltext.")
        if self.resolved_date_type() not in DATE_TYPES:
            raise ValueError(f"date_type must be one of {', '.join(DATE_TYPES)}")
        if bad := set(self.download_formats) - set(DOWNLOAD_FORMATS):
            raise ValueError(f"Unknown download format(s): {', '.join(sorted(bad))}")
        path, fmt = resolve_output(self.output_file, self.format)
        if path == STDOUT and (fmt == "sqlite" or self.merge or self.resume):
            raise ValueError("Writing to stdout ('-') can't be combined with sqlite, --merge or --resume.")
        if self.merge and self.resume:
            raise ValueError("--merge and --resume can't be used together.")


@dataclass
class RunResult:
    exit_code: int
    search_info: dict[str, Any]
    output: str


def run(options: Options, session: requests.Session | None = None) -> RunResult:
    """
    Execute a run. Returns a RunResult whose exit_code is 0 (complete) or 2 (some batches failed).
    Raises ValueError for invalid options and PubMedError if nothing could be fetched.
    """
    options.validate()
    client = PubMedClient(
        email=options.email, api_key=options.api_key, session=session, strict=options.strict, raw_dir=options.save_raw
    )
    fulltext = PMCFullText(client.session, client.timeout)
    out_path, fmt = resolve_output(options.output_file, options.format)
    date_type = options.resolved_date_type()
    # The human-readable listing goes to stdout, so it's off when the data itself goes there.
    display = options.display and out_path != STDOUT

    end: date | None = None
    batches: Iterator[list[Article]]
    if options.mode == "search":
        start_date = options.start_date
        if options.since_last_run:
            state = load_state(options.since_last_run)
            start_date = start_from_state(state, options.spec.build(), date_type)
            if start_date is None:
                log.info("No state file yet at %s; starting a new incremental series.", options.since_last_run)
        start, end = resolve_date_range(options.days, start_date, options.end_date)
        batches = client.iter_search(options.spec, start, end, date_type, options.max_results)
    elif options.mode == "pmids":
        batches = client.iter_pmids(options.pmids or [])
    else:
        batches = client.iter_raw(options.from_raw or "")

    stats = {"skipped_existing": 0, "fulltext_lookup_failures": 0, "icite_lookup_failures": 0}
    downloads = {"downloaded": 0, "failed": 0}
    sink: Sink | None = None
    shown = 0
    for batch in batches:
        # Open the output only once records arrive, so a failed search leaves no empty file behind.
        sink = sink or open_sink(out_path, fmt, merge=options.merge, resume=options.resume)
        if sink.existing_pmids:
            kept = [a for a in batch if a["pmid"] not in sink.existing_pmids]
            stats["skipped_existing"] += len(batch) - len(kept)
            batch = kept
        if not batch:
            continue
        if options.fulltext:
            stats["fulltext_lookup_failures"] += fulltext.annotate(batch)
        if options.download_dir:
            done, failed = fulltext.download(batch, options.download_dir, options.download_formats)
            downloads["downloaded"] += done
            downloads["failed"] += failed
        if options.icite:
            stats["icite_lookup_failures"] += add_icite_metrics(client.session, batch, client.timeout)
        if display:
            print_articles(batch, start=shown + 1)
            shown += len(batch)
        sink.write(batch)

    info: dict[str, Any] = {
        "tool": {"name": TOOL_NAME, "version": __version__},
        "schema_version": SCHEMA_VERSION,
        "generated_on": utc_now(),
        **client.search_info,
        **stats,
    }
    if options.download_dir:
        info["downloads"] = downloads
    info["complete"] = not info["failed_batches"]
    if info["failed_batches"] and not info["retrieved"]:
        raise PubMedError(f"Could not fetch any records: {info['failed_batches'][0]['error']}")
    if display and not shown:
        print("No new articles." if stats["skipped_existing"] else "No articles found.")

    sink = sink or open_sink(out_path, fmt, merge=options.merge, resume=options.resume)
    info["total_articles"] = sink.close(info)
    if out_path != STDOUT:
        log.info("Results saved to: %s (%d articles)", out_path, info["total_articles"])
    if options.summary:
        write_summary(options.summary, info)
    if options.since_last_run and end is not None:
        if not info["complete"]:
            log.warning("Not updating %s because some batches failed; the next run will retry.", options.since_last_run)
        elif info.get("truncated"):
            # Advancing past articles that weren't retrieved would skip them for good.
            log.warning(
                "Not updating %s because not all matching articles were retrieved; raise --max-results so the next "
                "run doesn't skip any.",
                options.since_last_run,
            )
        else:
            save_state(options.since_last_run, info.get("query"), date_type, end, info.get("total_found"))

    if not info["complete"]:
        log.warning("Run incomplete: %d batch(es) failed. See search_info.failed_batches.", len(info["failed_batches"]))
    return RunResult(EXIT_OK if info["complete"] else EXIT_PARTIAL, info, out_path)
