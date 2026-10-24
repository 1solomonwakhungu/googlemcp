"""Input validation shared by the reporting adapters (YouTube Analytics,
Google Analytics, Search Console).

Private module: ``discover_adapters`` skips names that start with ``_``.
Validation errors raise ``GoogleMCPError`` so the message reaches the model
instead of a confusing upstream 400.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import date

from googlemcp.services.base import GoogleMCPError

# Row ceiling for report-style tools. Larger than MAX_PAGE_SIZE because report
# rows are small, but still bounded to keep tool output reasonable.
MAX_REPORT_ROWS = 1000
DEFAULT_REPORT_ROWS = 100

_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_FIELD_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_:]{0,99}$")


def clamp_rows(value: int | None) -> int:
    if value is None:
        return DEFAULT_REPORT_ROWS
    return max(1, min(int(value), MAX_REPORT_ROWS))


def iso_date(value: str, field: str) -> date:
    """Parse a strict ``YYYY-MM-DD`` date or raise a readable error."""
    if not _ISO_DATE.match(value or ""):
        raise GoogleMCPError(f"{field} must be a date in YYYY-MM-DD form, got {value!r}")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise GoogleMCPError(f"{field} is not a valid calendar date: {value!r}") from exc


def date_range(start_date: str, end_date: str) -> tuple[str, str]:
    """Validate an inclusive ISO date range and return it unchanged."""
    start = iso_date(start_date, "start_date")
    end = iso_date(end_date, "end_date")
    if start > end:
        raise GoogleMCPError("start_date must be on or before end_date")
    return start_date, end_date


def field_names(
    values: Iterable[str],
    *,
    field: str,
    max_count: int,
    required: bool,
    allowed: frozenset[str] | None = None,
) -> list[str]:
    """Validate a list of metric or dimension names."""
    names = [v.strip() for v in values if v and v.strip()]
    if required and not names:
        raise GoogleMCPError(f"{field} must contain at least one name")
    if len(names) > max_count:
        raise GoogleMCPError(f"{field} accepts at most {max_count} names")
    if len(set(names)) != len(names):
        raise GoogleMCPError(f"{field} contains duplicates")
    for name in names:
        if allowed is not None and name not in allowed:
            raise GoogleMCPError(f"{field}: {name!r} is not one of {sorted(allowed)}")
        if not _FIELD_NAME.match(name):
            raise GoogleMCPError(f"{field}: {name!r} is not a valid name")
    return names
