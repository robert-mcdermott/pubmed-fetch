"""Build PubMed queries and date ranges."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, timedelta

from ._meta import PubMedError

# Search categories, in the order they appear in the query, and their PubMed field tags.
FIELD_TAGS = {
    "mesh_terms": "MeSH Terms",
    "authors": "Author",
    "organizations": "Affiliation",
    "title_abstract": "Title/Abstract",
    "journals": "Journal",
    "publication_types": "Publication Type",
    "languages": "Language",
}

DATE_TYPES = {
    "pdat": "publication date",
    "edat": "Entrez date (when the record was added to PubMed)",
    "mdat": "modification date",
}


def _quote(term: str) -> str:
    return '"' + term.replace('"', "").strip() + '"'


@dataclass
class QuerySpec:
    """
    A PubMed search: terms are ORed within each category and categories are ANDed.
    ``raw_query`` is ANDed in as-is and ``exclude`` expressions are removed with NOT.
    """

    mesh_terms: list[str] = field(default_factory=list)
    authors: list[str] = field(default_factory=list)
    organizations: list[str] = field(default_factory=list)
    title_abstract: list[str] = field(default_factory=list)
    journals: list[str] = field(default_factory=list)
    publication_types: list[str] = field(default_factory=list)
    languages: list[str] = field(default_factory=list)
    raw_query: str | None = None
    exclude: list[str] = field(default_factory=list)
    mesh_noexp: bool = False

    def build(self) -> str:
        parts = []
        for key, tag in FIELD_TAGS.items():
            terms = [t for t in getattr(self, key) if t.strip()]
            if not terms:
                continue
            if key == "mesh_terms" and self.mesh_noexp:
                tag = "MeSH Terms:noexp"
            parts.append("(" + " OR ".join(f"{_quote(t)}[{tag}]" for t in terms) + ")")
        if self.raw_query and self.raw_query.strip():
            parts.append(f"({self.raw_query.strip()})")
        query = " AND ".join(parts)
        exclude = [e.strip() for e in self.exclude if e.strip()]
        if query and exclude:
            query = f"({query}) NOT (" + " OR ".join(f"({e})" for e in exclude) + ")"
        return query

    def is_empty(self) -> bool:
        return not self.build()

    def filters(self) -> dict:
        return asdict(self)


def build_query(mesh_terms=None, authors=None, organizations=None, **filters) -> str:
    """Build a PubMed query: OR within each category, AND across categories."""
    return QuerySpec(
        mesh_terms=list(mesh_terms or []),
        authors=list(authors or []),
        organizations=list(organizations or []),
        **filters,
    ).build()


def resolve_date_range(
    days_back: int = 30, start_date: date | None = None, end_date: date | None = None, today: date | None = None
) -> tuple[date, date]:
    """
    Return (start, end) dates. Explicit dates win; a missing end defaults to today and a
    missing start defaults to ``days_back`` days before the end.
    """
    end = end_date or today or date.today()
    start = start_date or end - timedelta(days=days_back)
    if start > end:
        raise PubMedError(f"Start date {start} is after end date {end}.")
    return start, end
