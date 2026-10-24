from urllib.parse import parse_qs, urlparse

import pytest

from googlemcp.services.base import GoogleMCPError, UpstreamError
from googlemcp.services.people import ADAPTER
from googlemcp.testing import FakeContext, call, registry_for


def _query(uri: str) -> dict[str, list[str]]:
    return parse_qs(urlparse(uri).query)


def _tool(name: str):  # type: ignore[no-untyped-def]
    return next(t for t in registry_for(ADAPTER, FakeContext()).tools if t.name == name)


PERSON = {
    "resourceName": "people/c111",
    "etag": "%EgUBAi43PRoEAQIFByIMZm9v",
    "metadata": {
        "sources": [
            {
                "type": "CONTACT",
                "id": "111",
                "etag": "#source-etag",
                "updateTime": "2026-10-01T00:00:00Z",
            },
            {"type": "PROFILE", "id": "999", "etag": "#profile-etag"},
        ],
        "objectType": "PERSON",
    },
    "names": [
        {
            "displayName": "Ada Lovelace",
            "givenName": "Ada",
            "familyName": "Lovelace",
            "unstructuredName": "Ada Lovelace",
            "metadata": {"primary": True},
        }
    ],
    "emailAddresses": [{"value": "ada@example.com", "type": "work", "metadata": {}}],
    "phoneNumbers": [{"value": "+1 555 0100", "canonicalForm": "+15550100"}],
    "organizations": [{"name": "Analytical Engines", "title": "Engineer", "metadata": {}}],
    "biographies": [{"value": "n" * 3000}],
    "birthdays": [{"date": {"month": 12, "day": 10}}],
}

SHAPED = {
    "resource_name": "people/c111",
    "display_name": "Ada Lovelace",
    "given_name": "Ada",
    "family_name": "Lovelace",
    "emails": [{"value": "ada@example.com", "type": "work"}],
    "phones": [{"value": "+1 555 0100"}],
    "organizations": [{"name": "Analytical Engines", "title": "Engineer"}],
    "birthday": {"month": 12, "day": 10},
    "notes": "n" * 2000,
}


def test_search_contacts_warms_up_once_and_clamps() -> None:
    ctx = (
        FakeContext()
        .respond({})
        .respond({"results": [{"person": PERSON}]})
        .respond({"results": []})
    )
    reg = registry_for(ADAPTER, ctx)
    out = call(reg, "people_search_contacts", query="ada", max_results=100)
    call(reg, "people_search_contacts", query="bob")

    assert out == {"people": [SHAPED]}
    warmup, first, second = ctx.requests
    assert "/v1/people:searchContacts" in warmup.uri
    assert _query(warmup.uri).get("query", [""]) == [""]
    assert _query(first.uri)["query"] == ["ada"]
    assert _query(first.uri)["pageSize"] == ["30"]
    assert _query(first.uri)["readMask"] == ["names,emailAddresses,phoneNumbers,organizations"]
    assert _query(second.uri)["query"] == ["bob"]


def test_get_person_defaults_to_me_and_validates() -> None:
    ctx = FakeContext().respond(PERSON)
    out = call(registry_for(ADAPTER, ctx), "people_get_person")
    assert out == SHAPED
    assert "/v1/people/me?" in ctx.requests[0].uri
    assert "biographies" in _query(ctx.requests[0].uri)["personFields"][0]

    with pytest.raises(GoogleMCPError, match="resource_name"):
        call(
            registry_for(ADAPTER, FakeContext()),
            "people_get_person",
            resource_name="contactGroups/all",
        )


def test_list_connections() -> None:
    ctx = FakeContext().respond({"connections": [PERSON], "nextPageToken": "n", "totalItems": 7})
    out = call(registry_for(ADAPTER, ctx), "people_list_connections", max_results=5000)
    assert out["people"][0]["resource_name"] == "people/c111"
    assert out["next_page_token"] == "n"
    assert out["total_items"] == 7
    q = _query(ctx.requests[0].uri)
    assert "/v1/people/me/connections" in ctx.requests[0].uri
    assert q["pageSize"] == ["100"]
    assert q["sortOrder"] == ["LAST_MODIFIED_DESCENDING"]

    with pytest.raises(GoogleMCPError, match="sort_order"):
        call(registry_for(ADAPTER, FakeContext()), "people_list_connections", sort_order="RANDOM")


def test_other_contacts_list_and_search() -> None:
    ctx = (
        FakeContext()
        .respond(
            {
                "otherContacts": [
                    {"resourceName": "otherContacts/c9", "emailAddresses": [{"value": "x@y.z"}]}
                ]
            }
        )
        .respond({})
        .respond({"results": [{"person": {"resourceName": "otherContacts/c9"}}]})
    )
    reg = registry_for(ADAPTER, ctx)
    listed = call(reg, "people_list_other_contacts")
    found = call(reg, "people_search_other_contacts", query="x@")
    assert listed["people"] == [
        {"resource_name": "otherContacts/c9", "display_name": None, "emails": [{"value": "x@y.z"}]}
    ]
    assert found["people"][0]["resource_name"] == "otherContacts/c9"
    list_req, warmup, search_req = ctx.requests
    assert "/v1/otherContacts?" in list_req.uri
    assert _query(list_req.uri)["readMask"] == ["names,emailAddresses,phoneNumbers"]
    assert "/v1/otherContacts:search" in warmup.uri
    assert _query(search_req.uri)["query"] == ["x@"]


def test_create_contact_body() -> None:
    ctx = FakeContext().respond(PERSON)
    call(
        registry_for(ADAPTER, ctx),
        "people_create_contact",
        given_name="Ada",
        emails=["ada@example.com"],
        organization="Analytical Engines",
        notes="met at conf",
    )
    req = ctx.requests[0]
    assert req.method == "POST"
    assert "/v1/people:createContact" in req.uri
    assert req.body == {
        "names": [{"givenName": "Ada"}],
        "emailAddresses": [{"value": "ada@example.com"}],
        "organizations": [{"name": "Analytical Engines"}],
        "biographies": [{"value": "met at conf", "contentType": "TEXT_PLAIN"}],
    }
    with pytest.raises(GoogleMCPError):
        call(registry_for(ADAPTER, FakeContext()), "people_create_contact", notes="only notes")


def test_update_contact_fetches_etag_and_merges_name() -> None:
    ctx = FakeContext().respond(PERSON).respond(PERSON)
    call(
        registry_for(ADAPTER, ctx),
        "people_update_contact",
        resource_name="people/c111",
        family_name="King",
        phones=["+1 555 0199"],
    )
    get_req, update_req = ctx.requests
    assert get_req.method == "GET"
    assert _query(get_req.uri)["personFields"] == ["metadata,names,phoneNumbers"]
    assert update_req.method == "PATCH"
    assert "/v1/people/c111:updateContact" in update_req.uri
    assert _query(update_req.uri)["updatePersonFields"] == ["names,phoneNumbers"]
    # updateContact requires person.metadata.sources with the CONTACT source etag.
    assert update_req.body == {
        "etag": PERSON["etag"],
        "metadata": {"sources": [{"type": "CONTACT", "id": "111", "etag": "#source-etag"}]},
        "names": [{"givenName": "Ada", "familyName": "King"}],
        "phoneNumbers": [{"value": "+1 555 0199"}],
    }


def test_update_contact_without_contact_source_is_refused() -> None:
    profile_only = {**PERSON, "metadata": {"sources": [{"type": "PROFILE", "id": "9"}]}}
    ctx = FakeContext().respond(profile_only)
    with pytest.raises(GoogleMCPError, match="no contact source"):
        call(
            registry_for(ADAPTER, ctx),
            "people_update_contact",
            resource_name="people/c111",
            given_name="x",
        )
    assert len(ctx.requests) == 1


def test_update_and_delete_validate_resource_name() -> None:
    ctx = FakeContext()
    reg = registry_for(ADAPTER, ctx)
    with pytest.raises(GoogleMCPError, match="resource_name"):
        call(reg, "people_update_contact", resource_name="people/me", given_name="x")
    with pytest.raises(GoogleMCPError, match="resource_name"):
        call(reg, "people_delete_contact", resource_name="people/c1/../me")
    with pytest.raises(GoogleMCPError, match="nothing"):
        call(reg, "people_update_contact", resource_name="people/c1")
    assert ctx.requests == []


def test_delete_contact() -> None:
    ctx = FakeContext().respond("", status=200)
    out = call(registry_for(ADAPTER, ctx), "people_delete_contact", resource_name="people/c111")
    assert out == {"deleted": True, "resource_name": "people/c111"}
    assert ctx.requests[0].method == "DELETE"
    assert "/v1/people/c111:deleteContact" in ctx.requests[0].uri


def test_tool_flags_and_scopes() -> None:
    reads = (
        "people_search_contacts",
        "people_get_person",
        "people_list_connections",
        "people_list_other_contacts",
        "people_search_other_contacts",
    )
    for name in reads:
        assert not _tool(name).write
    for name in ("people_create_contact", "people_update_contact"):
        assert _tool(name).write and not _tool(name).destructive
    assert _tool("people_delete_contact").destructive
    assert ADAPTER.spec.write_scopes == ("https://www.googleapis.com/auth/contacts",)
    assert "https://www.googleapis.com/auth/directory.readonly" not in ADAPTER.spec.read_scopes


def test_upstream_error_propagates() -> None:
    ctx = FakeContext().respond({"error": {"code": 429}}, status=429)
    with pytest.raises(UpstreamError) as info:
        call(registry_for(ADAPTER, ctx), "people_list_connections")
    assert info.value.status == 429


def _with_orgs(orgs: list[dict[str, object]]) -> dict[str, object]:
    return {**PERSON, "organizations": orgs}


def test_update_keeps_other_organizations_and_edits_primary() -> None:
    current = _with_orgs(
        [
            {"name": "Side Gig", "title": "Advisor", "metadata": {"source": {"type": "CONTACT"}}},
            {
                "name": "Analytical Engines",
                "title": "Engineer",
                "department": "R&D",
                "formattedType": "Work",
                "type": "work",
                "metadata": {"primary": True},
            },
        ]
    )
    ctx = FakeContext().respond(current).respond(current)
    call(
        registry_for(ADAPTER, ctx),
        "people_update_contact",
        resource_name="people/c111",
        job_title="CTO",
    )
    update_req = ctx.requests[1]
    assert _query(update_req.uri)["updatePersonFields"] == ["organizations"]
    assert update_req.body["organizations"] == [
        {"name": "Side Gig", "title": "Advisor"},
        {"name": "Analytical Engines", "title": "CTO", "department": "R&D", "type": "work"},
    ]


def test_update_without_primary_flag_edits_first_organization() -> None:
    current = _with_orgs([{"name": "A"}, {"name": "B"}])
    ctx = FakeContext().respond(current).respond(current)
    call(
        registry_for(ADAPTER, ctx),
        "people_update_contact",
        resource_name="people/c111",
        organization="A2",
    )
    assert ctx.requests[1].body["organizations"] == [{"name": "A2"}, {"name": "B"}]
