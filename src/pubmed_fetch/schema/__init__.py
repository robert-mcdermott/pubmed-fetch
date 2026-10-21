"""JSON Schema for pubmed-fetch output (schema_version 2)."""

import json
from functools import cache
from importlib.resources import files
from typing import Any


@cache
def load_schema() -> dict[str, Any]:
    """The JSON Schema for a JSON output document. Article records are under ``$defs/article``."""
    return json.loads(files(__package__).joinpath("output.schema.json").read_text(encoding="utf-8"))


def article_schema() -> dict[str, Any]:
    """A standalone schema for one article record (e.g. a JSONL line)."""
    schema = load_schema()
    return {"$schema": schema["$schema"], "$defs": schema["$defs"], "$ref": "#/$defs/article"}
