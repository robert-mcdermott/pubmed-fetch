from importlib.metadata import PackageNotFoundError, version

TOOL_NAME = "pubmed-fetch"
# Bump when the structure of output records changes in a way consumers must handle.
SCHEMA_VERSION = 2

try:
    __version__ = version(TOOL_NAME)
except PackageNotFoundError:  # pragma: no cover - running from a source tree without installing
    __version__ = "0.0.0"


class PubMedError(Exception):
    """Raised when PubMed cannot be queried or returns an error."""


def utc_now() -> str:
    """Current time as an ISO-8601 UTC timestamp, e.g. 2024-01-31T12:00:00Z."""
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
