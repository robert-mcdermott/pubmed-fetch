"""Parse PubMed efetch XML into article records (output schema v2)."""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from typing import Any

log = logging.getLogger(__name__)

Article = dict[str, Any]

MONTHS = {
    m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)
}
ORCID_RE = re.compile(r"(\d{4}-?\d{4}-?\d{4}-?\d{3}[\dX])", re.IGNORECASE)
RETRACTED_TYPE = "Retracted Publication"


def text(elem: ET.Element | None) -> str:
    """All text inside an element, including text in inline markup such as <i> or <sup>."""
    return " ".join("".join(elem.itertext()).split()) if elem is not None else ""


def opt(elem: ET.Element | None) -> str | None:
    """Like :func:`text`, but None when the element is missing or empty."""
    return text(elem) or None


def _int(value: str) -> int | None:
    return int(value) if value.isdigit() else None


def _month(value: str) -> int | None:
    return MONTHS.get(value[:3].lower()) or _int(value)


def parse_date(elem: ET.Element | None) -> dict[str, Any]:
    """
    Parse a PubDate-like element into ``date`` (ISO-8601, as precise as known), ``year``,
    ``month``, ``day``, ``date_precision`` (day/month/year) and ``date_raw`` for dates that
    can't be represented exactly (e.g. "1998 Winter" or MedlineDate "2023 Jan-Feb").
    """
    result: dict[str, Any] = {
        "date": None,
        "year": None,
        "month": None,
        "day": None,
        "date_precision": None,
        "date_raw": None,
    }
    if elem is None:
        return result

    year = _int(text(elem.find("Year")))
    month = _month(text(elem.find("Month")))
    day = _int(text(elem.find("Day")))
    medline_date, season = text(elem.find("MedlineDate")), text(elem.find("Season"))
    if year is None and medline_date:
        # e.g. "1991 Dec 23-30" -> 1991-12; ranges like "2023 Jan-Feb" or "1998-1999" keep only the year.
        match = re.match(r"(\d{4})(?:\s+([A-Za-z]{3})[a-z]*(?![-/]))?", medline_date)
        if match:
            year = int(match.group(1))
            month = _month(match.group(2)) if match.group(2) else None
            day = None
    if year is None:
        result["date_raw"] = medline_date or None
        return result

    if month is None:
        day = None
    result.update(year=year, month=month, day=day)
    if day is not None:
        result.update(date=f"{year:04d}-{month:02d}-{day:02d}", date_precision="day")
    elif month is not None:
        result.update(date=f"{year:04d}-{month:02d}", date_precision="month")
    else:
        result.update(date=f"{year:04d}", date_precision="year")
    if medline_date:
        result["date_raw"] = medline_date
    elif season:
        result["date_raw"] = f"{year} {season}"
    return result


def _iso_date(elem: ET.Element | None) -> str | None:
    return parse_date(elem)["date"]


def _normalize_orcid(value: str) -> str | None:
    match = ORCID_RE.search(value.replace(" ", ""))
    if not match:
        return None
    digits = match.group(1).replace("-", "").upper()
    return "-".join(digits[i : i + 4] for i in range(0, 16, 4))


def parse_author(author: ET.Element, position: int) -> dict[str, Any] | None:
    last, fore, collective = (
        opt(author.find("LastName")),
        opt(author.find("ForeName")),
        opt(author.find("CollectiveName")),
    )
    name = (f"{last}, {fore}" if fore else last) if last else collective
    if not name:
        return None
    orcid = next(
        (o for i in author.findall("Identifier") if i.get("Source") == "ORCID" and (o := _normalize_orcid(text(i)))),
        None,
    )
    return {
        "position": position,
        "name": name,
        "last_name": last,
        "fore_name": fore,
        "initials": opt(author.find("Initials")),
        "collective_name": collective,
        "orcid": orcid,
        "affiliations": [a for e in author.findall("AffiliationInfo/Affiliation") if (a := text(e))],
    }


def _authors(author_list: ET.Element | None) -> list[dict[str, Any]]:
    if author_list is None:
        return []
    authors = (parse_author(a, i) for i, a in enumerate(author_list.findall("Author"), 1))
    return [a for a in authors if a]


def _authors_display(authors: list[dict[str, Any]]) -> str:
    names = [a["name"] for a in authors]
    return "; ".join(names[:5]) + (" et al." if len(names) > 5 else "")


def _abstract(container: ET.Element | None) -> tuple[str | None, list[dict[str, Any]]]:
    sections: list[dict[str, Any]] = []
    if container is not None:
        for elem in container.findall("Abstract/AbstractText"):
            if body := text(elem):
                sections.append({"label": elem.get("Label"), "category": elem.get("NlmCategory"), "text": body})
    abstract = " ".join(f"{s['label']}: {s['text']}" if s["label"] else str(s["text"]) for s in sections)
    return abstract or None, sections


def _article_ids(id_list: ET.Element | None) -> dict[str, str]:
    """The record's own IDs. Cited references carry their own ArticleIdList elsewhere and are ignored."""
    ids: dict[str, str] = {}
    if id_list is not None:
        for elem in id_list.findall("ArticleId"):
            if (id_type := elem.get("IdType")) and (value := text(elem)):
                ids.setdefault(id_type, value)
    return ids


def _texts(parent: ET.Element | None, path: str) -> list[str]:
    return [t for e in parent.findall(path) if (t := text(e))] if parent is not None else []


def _common(record_type: str, pmid: str) -> Article:
    return {
        "record_type": record_type,
        "pmid": pmid,
        "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
        "fulltext": None,
        "metrics": None,
    }


def _finish(article: Article, ids: dict[str, str], comments: list[dict[str, Any]]) -> Article:
    article["article_ids"] = ids
    article["doi"] = ids.get("doi") or article.get("doi")
    article["pmc_id"] = ids.get("pmc")
    article["comments_corrections"] = comments
    article["is_retracted"] = RETRACTED_TYPE in article["publication_types"] or any(
        c["type"] == "RetractionIn" for c in comments
    )
    article["authors_display"] = _authors_display(article["authors"])
    return article


def parse_journal_article(elem: ET.Element) -> Article | None:
    citation = elem.find("MedlineCitation")
    art = citation.find("Article") if citation is not None else None
    pmid = text(citation.find("PMID")) if citation is not None else ""
    if citation is None or art is None or not pmid:
        log.warning("Skipping malformed PubmedArticle record")
        return None

    journal, issue = art.find("Journal"), art.find("Journal/JournalIssue")
    abstract, sections = _abstract(art)
    epub = next((d for d in art.findall("ArticleDate") if d.get("DateType") == "Electronic"), None)
    issn = journal.find("ISSN") if journal is not None else None

    mesh = []
    for heading in citation.findall("MeshHeadingList/MeshHeading"):
        descriptor = heading.find("DescriptorName")
        if descriptor is None or not text(descriptor):
            continue
        mesh.append(
            {
                "descriptor": text(descriptor),
                "ui": descriptor.get("UI"),
                "major_topic": descriptor.get("MajorTopicYN") == "Y",
                "qualifiers": [
                    {"name": text(q), "ui": q.get("UI"), "major_topic": q.get("MajorTopicYN") == "Y"}
                    for q in heading.findall("QualifierName")
                ],
            }
        )

    comments = [
        {"type": c.get("RefType"), "pmid": opt(c.find("PMID")), "source": opt(c.find("RefSource"))}
        for c in citation.findall("CommentsCorrectionsList/CommentsCorrections")
    ]

    article = _common("journal_article", pmid)
    article.update(
        {
            "title": opt(art.find("ArticleTitle")) or opt(art.find("VernacularTitle")),
            "abstract": abstract,
            "abstract_sections": sections,
            "authors": _authors(art.find("AuthorList")),
            "journal": opt(art.find("Journal/Title")),
            "journal_abbrev": opt(art.find("Journal/ISOAbbreviation"))
            or opt(citation.find("MedlineJournalInfo/MedlineTA")),
            "issn": opt(issn),
            "issn_linking": opt(citation.find("MedlineJournalInfo/ISSNLinking")),
            "volume": opt(issue.find("Volume")) if issue is not None else None,
            "issue": opt(issue.find("Issue")) if issue is not None else None,
            "pages": opt(art.find("Pagination/MedlinePgn")),
            "book_title": None,
            "publisher": None,
            **parse_date(art.find("Journal/JournalIssue/PubDate")),
            "epub_date": _iso_date(epub),
            "languages": _texts(art, "Language"),
            "publication_types": _texts(art, "PublicationTypeList/PublicationType"),
            "publication_status": opt(elem.find("PubmedData/PublicationStatus")),
            "mesh": mesh,
            "mesh_terms": [m["descriptor"] for m in mesh],
            "keywords": _texts(citation, "KeywordList/Keyword"),
            "grants": [
                {
                    "grant_id": opt(g.find("GrantID")),
                    "acronym": opt(g.find("Acronym")),
                    "agency": opt(g.find("Agency")),
                    "country": opt(g.find("Country")),
                }
                for g in art.findall("GrantList/Grant")
            ],
            "doi": next((text(e) for e in art.findall("ELocationID") if e.get("EIdType") == "doi" and text(e)), None),
        }
    )
    return _finish(article, _article_ids(elem.find("PubmedData/ArticleIdList")), comments)


def parse_book_article(elem: ET.Element) -> Article | None:
    doc = elem.find("BookDocument")
    pmid = text(doc.find("PMID")) if doc is not None else ""
    if doc is None or not pmid:
        log.warning("Skipping malformed PubmedBookArticle record")
        return None

    book = doc.find("Book")
    abstract, sections = _abstract(doc)
    author_lists = doc.findall("AuthorList")
    author_list = next((a for a in author_lists if a.get("Type", "authors") == "authors"), None)
    book_title = opt(book.find("BookTitle")) if book is not None else None

    article = _common("book", pmid)
    article.update(
        {
            "title": opt(doc.find("ArticleTitle")) or book_title,
            "abstract": abstract,
            "abstract_sections": sections,
            "authors": _authors(author_list),
            "journal": None,
            "journal_abbrev": None,
            "issn": None,
            "issn_linking": None,
            "volume": opt(book.find("Volume")) if book is not None else None,
            "issue": None,
            "pages": None,
            "book_title": book_title,
            "publisher": opt(book.find("Publisher/PublisherName")) if book is not None else None,
            **parse_date(book.find("PubDate") if book is not None else None),
            "epub_date": _iso_date(doc.find("ContributionDate")),
            "languages": _texts(doc, "Language"),
            "publication_types": _texts(doc, "PublicationType"),
            "publication_status": opt(elem.find("PubmedBookData/PublicationStatus")),
            "mesh": [],
            "mesh_terms": [],
            "keywords": _texts(doc, "KeywordList/Keyword"),
            "grants": [],
            "doi": None,
        }
    )
    ids = {**_article_ids(doc.find("ArticleIdList")), **_article_ids(elem.find("PubmedBookData/ArticleIdList"))}
    return _finish(article, ids, [])


PARSERS = {"PubmedArticle": parse_journal_article, "PubmedBookArticle": parse_book_article}


def parse_article(elem: ET.Element) -> Article | None:
    """Parse a <PubmedArticle> or <PubmedBookArticle> element."""
    parser = PARSERS.get(elem.tag)
    return parser(elem) if parser else None


def parse_records(root: ET.Element) -> tuple[list[Article], int]:
    """Parse every record in a <PubmedArticleSet>. Returns (articles, number of records skipped)."""
    articles, skipped = [], 0
    for elem in root:
        if elem.tag not in PARSERS:
            continue
        article = parse_article(elem)
        if article:
            articles.append(article)
        else:
            skipped += 1
    return articles, skipped
