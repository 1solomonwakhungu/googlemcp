"""Google Sheets adapter.

Writes default to ``valueInputOption=RAW`` so values are stored literally.
``USER_ENTERED`` parses input as if typed in the UI, which evaluates formulas
such as ``IMPORTDATA`` or ``IMAGE`` that can fetch external URLs; callers must
opt into it explicitly.

Scopes: ``spreadsheets.readonly`` for reads, ``spreadsheets`` for writes.
Reference: https://developers.google.com/workspace/sheets/api/scopes
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from googlemcp.services.base import (
    JSON,
    GoogleMCPError,
    ServiceContext,
    ServiceSpec,
    ToolRegistry,
    pick,
)

SPEC = ServiceSpec(
    key="sheets",
    title="Google Sheets",
    api_services=("sheets.googleapis.com",),
    read_scopes=("https://www.googleapis.com/auth/spreadsheets.readonly",),
    write_scopes=("https://www.googleapis.com/auth/spreadsheets",),
)

DEFAULT_MAX_ROWS = 200
MAX_ROWS_CEILING = 2_000
MAX_COLUMNS = 52
MAX_CELL_CHARS = 1_000
MAX_OUTPUT_CHARS = 200_000
MAX_WRITE_CELLS = 10_000
_VALUE_INPUT_OPTIONS = ("RAW", "USER_ENTERED")
_RENDER_OPTIONS = ("FORMATTED_VALUE", "UNFORMATTED_VALUE", "FORMULA")
_METADATA_FIELDS = (
    "spreadsheetId,spreadsheetUrl,"
    "properties(title,locale,timeZone),"
    "sheets(properties(sheetId,title,index,sheetType,gridProperties(rowCount,columnCount)))"
)
_RESOLVE_FIELDS = "sheets.properties(sheetId,title),namedRanges(name,range)"
_CELL = re.compile(r"^\$?([A-Za-z]{0,3})\$?(\d*)$")


@dataclass(frozen=True)
class GridBox:
    """A1 range as 1-based inclusive bounds; ``None`` end means open-ended."""

    prefix: str
    start_row: int
    start_col: int
    end_row: int | None
    end_col: int | None


def _col_number(letters: str) -> int:
    n = 0
    for ch in letters.upper():
        n = n * 26 + (ord(ch) - ord("A") + 1)
    return n


def _col_letters(n: int) -> str:
    out = ""
    while n:
        n, rem = divmod(n - 1, 26)
        out = chr(ord("A") + rem) + out
    return out


def _quote_sheet(title: str) -> str:
    return "'" + title.replace("'", "''") + "'"


def parse_a1(a1_range: str) -> GridBox | None:
    """Parse 'Sheet!A1:D20', 'A:D', '2:9', 'B5', or a quoted sheet name alone.

    Returns None when there is no parseable cell part (a bare token, which is a
    sheet title or a named range, or malformed input).
    """
    if a1_range.startswith("'") and a1_range.endswith("'") and len(a1_range) > 1:
        return GridBox(f"{a1_range}!", 1, 1, None, None)
    sheet, sep, cells = a1_range.rpartition("!")
    prefix = f"{sheet}!" if sep else ""
    start, colon, end = cells.partition(":")
    m1 = _CELL.match(start)
    if not m1 or not (m1[1] or m1[2]):
        return None
    if not colon:
        if not (m1[1] and m1[2]):
            return None
        row, col = int(m1[2]), _col_number(m1[1])
        return GridBox(prefix, row, col, row, col)
    m2 = _CELL.match(end)
    if not m2 or not (m2[1] or m2[2]) or bool(m1[1]) != bool(m2[1]):
        return None
    return GridBox(
        prefix,
        int(m1[2]) if m1[2] else 1,
        _col_number(m1[1]) if m1[1] else 1,
        int(m2[2]) if m2[2] else None,
        _col_number(m2[1]) if m2[1] else None,
    )


def bounded_a1(box: GridBox, rows: int, cols: int) -> str:
    """Render ``box`` limited to ``rows`` rows and ``cols`` columns."""
    end_row = box.start_row + rows - 1
    if box.end_row is not None:
        end_row = min(end_row, box.end_row)
    end_col = box.start_col + cols - 1
    if box.end_col is not None:
        end_col = min(end_col, box.end_col)
    start = f"{_col_letters(box.start_col)}{box.start_row}"
    return f"{box.prefix}{start}:{_col_letters(end_col)}{end_row}"


def _box_from_metadata(token: str, meta: JSON) -> GridBox | None:
    """Resolve a bare sheet title or named range using spreadsheet metadata."""
    titles = {}
    for sheet in meta.get("sheets", []):
        props = sheet.get("properties", {})
        titles[props.get("sheetId", 0)] = props.get("title", "")
    if token in titles.values():
        return GridBox(f"{_quote_sheet(token)}!", 1, 1, None, None)
    for named in meta.get("namedRanges", []):
        if named.get("name") != token:
            continue
        grid = named.get("range", {})
        title = titles.get(grid.get("sheetId", 0))
        if title is None:
            return None
        end_row = grid.get("endRowIndex")
        end_col = grid.get("endColumnIndex")
        return GridBox(
            f"{_quote_sheet(title)}!",
            grid.get("startRowIndex", 0) + 1,
            grid.get("startColumnIndex", 0) + 1,
            end_row,
            end_col,
        )
    return None


def _shape_rows(rows: list[list[Any]], limit: int) -> tuple[list[list[Any]], JSON]:
    """Apply row, column, cell, and total-size caps; report what was cut."""
    flags: JSON = {
        "truncated": len(rows) > limit,
        "columns_truncated": any(len(r) > MAX_COLUMNS for r in rows),
        "cells_truncated": False,
    }
    out: list[list[Any]] = []
    used = 0
    for row in rows[:limit]:
        shaped = []
        for cell in row[:MAX_COLUMNS]:
            if isinstance(cell, str) and len(cell) > MAX_CELL_CHARS:
                cell = cell[:MAX_CELL_CHARS]
                flags["cells_truncated"] = True
            shaped.append(cell)
        used += sum(len(str(c)) for c in shaped)
        if used > MAX_OUTPUT_CHARS:
            flags["truncated"] = True
            break
        out.append(shaped)
    return out, flags


def _check_choice(name: str, value: str, allowed: tuple[str, ...]) -> str:
    if value not in allowed:
        raise GoogleMCPError(f"{name} must be one of {', '.join(allowed)}")
    return value


def _check_values(values: list[list[Any]]) -> list[list[Any]]:
    if not values or not all(isinstance(row, list) for row in values):
        raise GoogleMCPError("values must be a non-empty list of rows (lists)")
    cells = sum(len(row) for row in values)
    if cells > MAX_WRITE_CELLS:
        raise GoogleMCPError(f"refusing to write {cells} cells; limit is {MAX_WRITE_CELLS}")
    return values


def _update_summary(resp: JSON) -> JSON:
    return {
        "updated_range": resp.get("updatedRange"),
        "updated_rows": resp.get("updatedRows", 0),
        "updated_columns": resp.get("updatedColumns", 0),
        "updated_cells": resp.get("updatedCells", 0),
    }


class SheetsAdapter:
    spec = SPEC

    def register(self, registry: ToolRegistry, ctx: ServiceContext) -> None:
        @registry.tool("sheets_get_spreadsheet")
        def get_spreadsheet(spreadsheet_id: str) -> JSON:
            """Get spreadsheet metadata: title, locale, time zone, and its sheets (tabs)."""
            svc = ctx.client("sheets", "v4")
            resp = ctx.execute(
                svc.spreadsheets().get(spreadsheetId=spreadsheet_id, fields=_METADATA_FIELDS)
            )
            props = resp.get("properties", {})
            return {
                "spreadsheet_id": resp.get("spreadsheetId", spreadsheet_id),
                "url": resp.get("spreadsheetUrl"),
                "title": props.get("title"),
                "locale": props.get("locale"),
                "time_zone": props.get("timeZone"),
                "sheets": [
                    {
                        **pick(s.get("properties", {}), ("sheetId", "title", "index", "sheetType")),
                        "rows": s.get("properties", {}).get("gridProperties", {}).get("rowCount"),
                        "columns": s.get("properties", {})
                        .get("gridProperties", {})
                        .get("columnCount"),
                    }
                    for s in resp.get("sheets", [])
                ],
            }

        @registry.tool("sheets_read_range")
        def read_range(
            spreadsheet_id: str,
            a1_range: str,
            value_render_option: str = "FORMATTED_VALUE",
            max_rows: int = DEFAULT_MAX_ROWS,
        ) -> JSON:
            """Read cell values from an A1 range (``a1_range``) such as 'Sheet1!A1:D20'.

            Accepts A1 ranges, a sheet title, or a named range. The request is
            bounded before it is sent: at most ``max_rows`` rows (default 200,
            ceiling 2000) and 52 columns, cell text capped at 1000 characters.
            ``truncated``, ``columns_truncated``, and ``cells_truncated`` report cuts.
            """
            _check_choice("value_render_option", value_render_option, _RENDER_OPTIONS)
            limit = max(1, min(int(max_rows), MAX_ROWS_CEILING))
            svc = ctx.client("sheets", "v4")
            box = parse_a1(a1_range)
            if box is None and "!" not in a1_range and a1_range.strip():
                meta = ctx.execute(
                    svc.spreadsheets().get(spreadsheetId=spreadsheet_id, fields=_RESOLVE_FIELDS)
                )
                box = _box_from_metadata(a1_range, meta)
            if box is None:
                raise GoogleMCPError(
                    f"unsupported range {a1_range!r}; use A1 notation such as "
                    "'Sheet1!A1:D100', a sheet title, or a named range"
                )
            # One extra row and column detect truncation without fetching more.
            request_range = bounded_a1(box, limit + 1, MAX_COLUMNS + 1)
            resp = ctx.execute(
                svc.spreadsheets()
                .values()
                .get(
                    spreadsheetId=spreadsheet_id,
                    range=request_range,
                    valueRenderOption=value_render_option,
                )
            )
            values, flags = _shape_rows(resp.get("values", []), limit)
            return {
                "range": resp.get("range", request_range),
                "values": values,
                "rows_returned": len(values),
                **flags,
            }

        @registry.tool("sheets_update_values", write=True, idempotent=True)
        def update_values(
            spreadsheet_id: str,
            a1_range: str,
            values: list[list[Any]],
            value_input_option: str = "RAW",
        ) -> JSON:
            """Overwrite cells in ``a1_range`` with ``values`` (a list of rows).

            ``value_input_option`` is RAW by default; USER_ENTERED evaluates formulas.
            """
            _check_choice("value_input_option", value_input_option, _VALUE_INPUT_OPTIONS)
            svc = ctx.client("sheets", "v4")
            resp = ctx.execute(
                svc.spreadsheets()
                .values()
                .update(
                    spreadsheetId=spreadsheet_id,
                    range=a1_range,
                    valueInputOption=value_input_option,
                    body={"range": a1_range, "values": _check_values(values)},
                )
            )
            return _update_summary(resp)

        @registry.tool("sheets_append_values", write=True)
        def append_values(
            spreadsheet_id: str,
            a1_range: str,
            values: list[list[Any]],
            value_input_option: str = "RAW",
        ) -> JSON:
            """Append rows after the last row of the table found in ``a1_range``.

            New rows are inserted (INSERT_ROWS) so existing data is never overwritten.
            """
            _check_choice("value_input_option", value_input_option, _VALUE_INPUT_OPTIONS)
            svc = ctx.client("sheets", "v4")
            resp = ctx.execute(
                svc.spreadsheets()
                .values()
                .append(
                    spreadsheetId=spreadsheet_id,
                    range=a1_range,
                    valueInputOption=value_input_option,
                    insertDataOption="INSERT_ROWS",
                    body={"values": _check_values(values)},
                )
            )
            return {
                "table_range": resp.get("tableRange"),
                **_update_summary(resp.get("updates", {})),
            }

        @registry.tool("sheets_create_spreadsheet", write=True)
        def create_spreadsheet(title: str, sheet_titles: list[str] | None = None) -> JSON:
            """Create a new spreadsheet, optionally with named sheets (tabs)."""
            body: JSON = {"properties": {"title": title}}
            if sheet_titles:
                body["sheets"] = [{"properties": {"title": t}} for t in sheet_titles]
            svc = ctx.client("sheets", "v4")
            resp = ctx.execute(svc.spreadsheets().create(body=body))
            return {
                "spreadsheet_id": resp.get("spreadsheetId"),
                "url": resp.get("spreadsheetUrl"),
                "title": resp.get("properties", {}).get("title", title),
                "sheets": [s.get("properties", {}).get("title") for s in resp.get("sheets", [])],
            }


ADAPTER = SheetsAdapter()
