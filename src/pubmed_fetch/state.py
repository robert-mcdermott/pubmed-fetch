"""State for incremental runs (--since-last-run)."""

from __future__ import annotations

import json
import logging
import os
from datetime import date
from typing import Any

from ._meta import utc_now

log = logging.getLogger(__name__)


def load_state(path: str) -> dict[str, Any] | None:
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def start_from_state(state: dict[str, Any] | None, query: str | None, date_type: str) -> date | None:
    """
    The start date for the next incremental run: the previous run's end date. Starting on that
    day (rather than the day after) overlaps by one day so nothing indexed late that day is
    missed; use --merge or SQLite output so the overlap replaces records instead of duplicating them.
    """
    if not state:
        return None
    if state.get("query") != query:
        log.warning("The state file was written for a different query; continuing from its end date anyway.")
    if (previous := state.get("date_type")) and previous != date_type:
        # Incremental runs used to default to edat, so older state files say edat.
        log.warning(
            "The state file was written for --date-type %s, but this run uses %s; continuing from its end date. "
            "Pass --date-type %s to keep the previous behavior.",
            previous,
            date_type,
            previous,
        )
    return date.fromisoformat(state["last_end_date"])


def save_state(path: str, query: str | None, date_type: str, end_date: date, total_found: int | None) -> None:
    state = {
        "last_end_date": end_date.isoformat(),
        "query": query,
        "date_type": date_type,
        "last_total_found": total_found,
        "updated_at": utc_now(),
    }
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, path)
