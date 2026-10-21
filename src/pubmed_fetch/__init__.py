"""
Search PubMed and export article metadata, abstracts and PubMed Central full-text links.

Library use::

    from pubmed_fetch import Options, QuerySpec, run

    result = run(Options(spec=QuerySpec(mesh_terms=["Breast Neoplasms"]), days=7, output_file="out.jsonl"))

or, for records in memory::

    from pubmed_fetch import PubMedClient

    articles = PubMedClient(email="you@example.com").search_pubmed(mesh_terms=["Breast Neoplasms"], days_back=7)
"""

import logging

from ._meta import SCHEMA_VERSION, PubMedError, __version__
from .client import PUBMED_RECORD_LIMIT, PubMedClient, SearchResult, make_session
from .fulltext import PMCFullText
from .icite import add_icite_metrics
from .outputs import open_sink, resolve_output, save_results
from .parse import parse_article, parse_records
from .pipeline import Options, RunResult, run
from .query import QuerySpec, build_query, resolve_date_range

logging.getLogger(__name__).addHandler(logging.NullHandler())

__all__ = [
    "PUBMED_RECORD_LIMIT",
    "SCHEMA_VERSION",
    "Options",
    "PMCFullText",
    "PubMedClient",
    "PubMedError",
    "QuerySpec",
    "RunResult",
    "SearchResult",
    "__version__",
    "add_icite_metrics",
    "build_query",
    "make_session",
    "open_sink",
    "parse_article",
    "parse_records",
    "resolve_date_range",
    "resolve_output",
    "run",
    "save_results",
]
