from urllib.parse import parse_qs, unquote, urlparse

import pytest

from googlemcp.services.base import GoogleMCPError, UpstreamError
from googlemcp.services.sheets import (
    ADAPTER,
    MAX_CELL_CHARS,
    MAX_COLUMNS,
    MAX_WRITE_CELLS,
    bounded_a1,
    parse_a1,
)
from googlemcp.testing import FakeContext, call, registry_for


def _query(uri: str) -> dict[str, list[str]]:
    return parse_qs(urlparse(uri).query)


def test_get_spreadsheet_requests_field_mask_and_shapes() -> None:
    ctx = FakeContext().respond(
        {
            "spreadsheetId": "s1",
            "spreadsheetUrl": "https://docs.google.com/spreadsheets/d/s1",
            "properties": {"title": "Budget", "locale": "en_US", "timeZone": "UTC"},
            "sheets": [
                {
                    "properties": {
                        "sheetId": 0,
                        "title": "Sheet1",
                        "index": 0,
                        "sheetType": "GRID",
                        "gridProperties": {"rowCount": 1000, "columnCount": 26},
                    }
                }
            ],
        }
    )
    out = call(registry_for(ADAPTER, ctx), "sheets_get_spreadsheet", spreadsheet_id="s1")

    assert out["title"] == "Budget"
    assert out["sheets"] == [
        {
            "sheetId": 0,
            "title": "Sheet1",
            "index": 0,
            "sheetType": "GRID",
            "rows": 1000,
            "columns": 26,
        }
    ]
    q = _query(ctx.requests[0].uri)
    assert "sheets(properties" in q["fields"][0]
    assert "includeGridData" not in q


def test_update_values_defaults_to_raw() -> None:
    ctx = FakeContext().respond(
        {"updatedRange": "Sheet1!A1:B1", "updatedRows": 1, "updatedColumns": 2, "updatedCells": 2}
    )
    out = call(
        registry_for(ADAPTER, ctx),
        "sheets_update_values",
        spreadsheet_id="s1",
        a1_range="Sheet1!A1:B1",
        values=[["=1+1", "x"]],
    )
    req = ctx.requests[0]
    assert req.method == "PUT"
    assert _query(req.uri)["valueInputOption"] == ["RAW"]
    assert req.body == {"range": "Sheet1!A1:B1", "values": [["=1+1", "x"]]}
    assert out["updated_cells"] == 2


def test_append_values_inserts_rows() -> None:
    ctx = FakeContext().respond(
        {
            "tableRange": "Sheet1!A1:B4",
            "updates": {"updatedRange": "Sheet1!A5:B5", "updatedCells": 2},
        }
    )
    out = call(
        registry_for(ADAPTER, ctx),
        "sheets_append_values",
        spreadsheet_id="s1",
        a1_range="Sheet1!A1",
        values=[["new", 5]],
        value_input_option="USER_ENTERED",
    )
    q = _query(ctx.requests[0].uri)
    assert ctx.requests[0].method == "POST"
    assert ":append" in ctx.requests[0].uri
    assert q["insertDataOption"] == ["INSERT_ROWS"]
    assert q["valueInputOption"] == ["USER_ENTERED"]
    assert out["table_range"] == "Sheet1!A1:B4"
    assert out["updated_range"] == "Sheet1!A5:B5"


def test_create_spreadsheet_body() -> None:
    ctx = FakeContext().respond(
        {
            "spreadsheetId": "s2",
            "spreadsheetUrl": "u",
            "properties": {"title": "New"},
            "sheets": [{"properties": {"title": "Data"}}],
        }
    )
    out = call(
        registry_for(ADAPTER, ctx), "sheets_create_spreadsheet", title="New", sheet_titles=["Data"]
    )
    assert ctx.requests[0].body == {
        "properties": {"title": "New"},
        "sheets": [{"properties": {"title": "Data"}}],
    }
    assert out == {"spreadsheet_id": "s2", "url": "u", "title": "New", "sheets": ["Data"]}


def test_validation_rejects_bad_options_and_oversized_writes() -> None:
    ctx = FakeContext()
    reg = registry_for(ADAPTER, ctx)
    with pytest.raises(GoogleMCPError):
        call(
            reg,
            "sheets_update_values",
            spreadsheet_id="s",
            a1_range="A1",
            values=[["x"]],
            value_input_option="INPUT_VALUE_OPTION_UNSPECIFIED",
        )
    with pytest.raises(GoogleMCPError):
        call(reg, "sheets_append_values", spreadsheet_id="s", a1_range="A1", values=[])
    with pytest.raises(GoogleMCPError):
        call(
            reg,
            "sheets_update_values",
            spreadsheet_id="s",
            a1_range="A1",
            values=[["x"] * (MAX_WRITE_CELLS + 1)],
        )
    with pytest.raises(GoogleMCPError):
        call(
            reg, "sheets_read_range", spreadsheet_id="s", a1_range="A1", value_render_option="BOGUS"
        )
    assert ctx.requests == []


def test_write_flags() -> None:
    reg = registry_for(ADAPTER, FakeContext())
    writes = {t.name for t in reg.tools if t.write}
    assert writes == {"sheets_update_values", "sheets_append_values", "sheets_create_spreadsheet"}
    assert not any(t.destructive for t in reg.tools)


def test_upstream_error_maps() -> None:
    ctx = FakeContext().respond({"error": {"code": 403}}, status=403)
    with pytest.raises(UpstreamError) as info:
        call(registry_for(ADAPTER, ctx), "sheets_read_range", spreadsheet_id="s", a1_range="A1")
    assert info.value.status == 403


def _requested_range(uri: str) -> str:
    return unquote(urlparse(uri).path.split("/values/", 1)[1])


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("Sheet1!A:D", "Sheet1!A1:D11"),
        ("Sheet1!A1:D5", "Sheet1!A1:D5"),
        ("Sheet1!A1:D500", "Sheet1!A1:D11"),
        ("A2:D", "A2:D12"),
        ("Sheet1!2:900", "Sheet1!A2:E12"),
        ("Sheet1!B5", "Sheet1!B5:B5"),
        ("'Q1 ''24'", "'Q1 ''24'!A1:E11"),
        ("'a!b'!C:C", "'a!b'!C1:C11"),
        ("$A$1:$B", "A1:B11"),
        ("Sheet1!A1:ZZ3", "Sheet1!A1:E3"),
        ("Sheet1!AA1:AB2", "Sheet1!AA1:AB2"),
    ],
)
def test_parse_and_bound(given: str, expected: str) -> None:
    box = parse_a1(given)
    assert box is not None
    assert bounded_a1(box, 11, 5) == expected


@pytest.mark.parametrize("given", ["Sheet1", "Named", "Sheet1!", "Sheet1!A", "A1:5", ""])
def test_parse_rejects_non_a1(given: str) -> None:
    assert parse_a1(given) is None


def test_read_range_trims_rows_and_flags() -> None:
    ctx = FakeContext().respond({"range": "Sheet1!A1:B3", "values": [["a", 1], ["b", 2], ["c", 3]]})
    out = call(
        registry_for(ADAPTER, ctx),
        "sheets_read_range",
        spreadsheet_id="s1",
        a1_range="Sheet1!A1:B3",
        max_rows=2,
    )
    assert out == {
        "range": "Sheet1!A1:B3",
        "values": [["a", 1], ["b", 2]],
        "rows_returned": 2,
        "truncated": True,
        "columns_truncated": False,
        "cells_truncated": False,
    }
    assert _requested_range(ctx.requests[0].uri) == "Sheet1!A1:B3"
    assert _query(ctx.requests[0].uri)["valueRenderOption"] == ["FORMATTED_VALUE"]


def test_read_range_bounds_open_ended_request() -> None:
    ctx = FakeContext().respond({"values": [[str(i)] for i in range(11)]})
    out = call(
        registry_for(ADAPTER, ctx),
        "sheets_read_range",
        spreadsheet_id="s1",
        a1_range="Sheet1!A:D",
        max_rows=10,
    )
    assert _requested_range(ctx.requests[0].uri) == "Sheet1!A1:D11"
    assert out["rows_returned"] == 10 and out["truncated"] is True


def test_read_range_row_only_range_bounds_columns() -> None:
    wide = [["x"] * (MAX_COLUMNS + 1)]
    ctx = FakeContext().respond({"values": wide})
    out = call(
        registry_for(ADAPTER, ctx), "sheets_read_range", spreadsheet_id="s1", a1_range="Sheet1!1:1"
    )
    assert _requested_range(ctx.requests[0].uri) == "Sheet1!A1:BA1"
    assert out["columns_truncated"] is True
    assert len(out["values"][0]) == MAX_COLUMNS
    assert out["truncated"] is False


def test_read_range_caps_cell_text() -> None:
    ctx = FakeContext().respond({"values": [["y" * (MAX_CELL_CHARS + 5), 7]]})
    out = call(
        registry_for(ADAPTER, ctx), "sheets_read_range", spreadsheet_id="s1", a1_range="A1:B1"
    )
    assert out["cells_truncated"] is True
    assert out["values"] == [["y" * MAX_CELL_CHARS, 7]]


def test_read_range_output_budget_stops_rows() -> None:
    big_row = ["z" * MAX_CELL_CHARS] * MAX_COLUMNS
    ctx = FakeContext().respond({"values": [big_row] * 10})
    out = call(
        registry_for(ADAPTER, ctx), "sheets_read_range", spreadsheet_id="s1", a1_range="A1:BA10"
    )
    assert out["truncated"] is True
    assert 0 < out["rows_returned"] < 10


def test_read_range_bare_sheet_title_is_bounded() -> None:
    ctx = (
        FakeContext()
        .respond({"sheets": [{"properties": {"sheetId": 0, "title": "Data"}}]})
        .respond({"values": [["x"]]})
    )
    call(
        registry_for(ADAPTER, ctx),
        "sheets_read_range",
        spreadsheet_id="s1",
        a1_range="Data",
        max_rows=5,
    )
    assert _query(ctx.requests[0].uri)["fields"] == [
        "sheets.properties(sheetId,title),namedRanges(name,range)"
    ]
    assert _requested_range(ctx.requests[1].uri) == "'Data'!A1:BA6"


def test_read_range_named_range_is_resolved_and_bounded() -> None:
    ctx = (
        FakeContext()
        .respond(
            {
                "sheets": [
                    {"properties": {"sheetId": 0, "title": "Data"}},
                    {"properties": {"sheetId": 7, "title": "Totals sheet"}},
                ],
                "namedRanges": [
                    {
                        "name": "Totals",
                        "range": {
                            "sheetId": 7,
                            "startRowIndex": 1,
                            "startColumnIndex": 1,
                            "endColumnIndex": 3,
                        },
                    }
                ],
            }
        )
        .respond({"values": [["x"]]})
    )
    call(
        registry_for(ADAPTER, ctx),
        "sheets_read_range",
        spreadsheet_id="s1",
        a1_range="Totals",
        max_rows=10,
    )
    assert _requested_range(ctx.requests[1].uri) == "'Totals sheet'!B2:C12"


def test_read_range_rejects_unresolvable_before_values_call() -> None:
    ctx = FakeContext().respond({"sheets": [{"properties": {"sheetId": 0, "title": "Data"}}]})
    reg = registry_for(ADAPTER, ctx)
    with pytest.raises(GoogleMCPError):
        call(reg, "sheets_read_range", spreadsheet_id="s1", a1_range="Nope")
    assert len(ctx.requests) == 1  # metadata only, no values request
    with pytest.raises(GoogleMCPError):
        call(reg, "sheets_read_range", spreadsheet_id="s1", a1_range="Sheet1!A")
    assert len(ctx.requests) == 1
