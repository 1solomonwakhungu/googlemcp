"""Google People (Contacts) adapter.

Scope choices (per-method scopes from the People v1 discovery document,
https://developers.google.com/people/v1/how-tos/authorizing):

- Read: ``contacts.readonly`` for ``people.searchContacts``, ``people.get``
  and ``people.connections.list``; ``contacts.other.readonly`` for
  ``otherContacts.list`` and ``otherContacts.search`` (addresses Gmail saved
  automatically). No other scope grants "Other contacts".
- Write: ``contacts`` for ``createContact``, ``updateContact`` and
  ``deleteContact``; it is the only scope that allows contact mutation.

``directory.readonly`` (Workspace domain directory) is intentionally not
requested: no tool here reads the domain directory, and consumer @gmail.com
accounts do not have one.
"""

from __future__ import annotations

import re
from typing import Any

from googlemcp.services.base import (
    JSON,
    GoogleMCPError,
    ServiceContext,
    ServiceSpec,
    ToolRegistry,
    clamp_page_size,
    pick,
)

SPEC = ServiceSpec(
    key="people",
    title="People and Contacts",
    api_services=("people.googleapis.com",),
    read_scopes=(
        "https://www.googleapis.com/auth/contacts.readonly",
        "https://www.googleapis.com/auth/contacts.other.readonly",
    ),
    write_scopes=("https://www.googleapis.com/auth/contacts",),
)

LIST_FIELDS = "names,emailAddresses,phoneNumbers,organizations"
DETAIL_FIELDS = f"{LIST_FIELDS},addresses,birthdays,biographies,urls,memberships"
OTHER_CONTACT_FIELDS = "names,emailAddresses,phoneNumbers"
# searchContacts and otherContacts.search cap pageSize at 30.
MAX_SEARCH_RESULTS = 30
MAX_BIO_CHARS = 2_000
SORT_ORDERS = frozenset(
    {
        "LAST_MODIFIED_ASCENDING",
        "LAST_MODIFIED_DESCENDING",
        "FIRST_NAME_ASCENDING",
        "LAST_NAME_ASCENDING",
    }
)
# Structured parts kept when merging an update. Server-derived fields such as
# displayName and unstructuredName are dropped so they cannot override new parts.
_NAME_KEYS = ("givenName", "familyName", "middleName", "honorificPrefix", "honorificSuffix")
# Output-only or server-managed fields stripped before writing entries back.
_SERVER_FIELDS = frozenset({"metadata", "formattedType"})
_PERSON_RE = re.compile(r"^people/(me|[A-Za-z0-9_-]+)$")
_CONTACT_RE = re.compile(r"^people/c[0-9]+$")


def _resource(value: str, *, contact: bool) -> str:
    pattern = _CONTACT_RE if contact else _PERSON_RE
    if not pattern.match(value):
        expected = "people/c<digits>" if contact else "people/me or people/<id>"
        raise GoogleMCPError(f"resource_name must look like {expected}")
    return value


def _values(person: JSON, field: str, keys: tuple[str, ...]) -> list[JSON]:
    out = []
    for entry in person.get(field, []) or []:
        item = {k: entry[k] for k in keys if entry.get(k)}
        if item:
            out.append(item)
    return out


def shape_person(person: JSON) -> JSON:
    names = person.get("names") or [{}]
    name = names[0]
    out: JSON = {
        "resource_name": person.get("resourceName"),
        "display_name": name.get("displayName"),
    }
    for src, dst in (("givenName", "given_name"), ("familyName", "family_name")):
        if name.get(src):
            out[dst] = name[src]
    for field, dst, keys in (
        ("emailAddresses", "emails", ("value", "type")),
        ("phoneNumbers", "phones", ("value", "type")),
        ("organizations", "organizations", ("name", "title")),
        ("addresses", "addresses", ("formattedValue", "type")),
        ("urls", "urls", ("value", "type")),
    ):
        values = _values(person, field, keys)
        if values:
            out[dst] = values
    birthdays = person.get("birthdays") or []
    if birthdays and "date" in birthdays[0]:
        out["birthday"] = birthdays[0]["date"]
    bios = person.get("biographies") or []
    if bios and bios[0].get("value"):
        out["notes"] = str(bios[0]["value"])[:MAX_BIO_CHARS]
    return out


def merge_name(stored: JSON, update: JSON) -> JSON:
    """Names are a singleton for contacts. Keep structured parts only so Google
    recomputes displayName and unstructuredName from the new values."""
    return {**{k: stored[k] for k in _NAME_KEYS if k in stored}, **update}


def merge_organizations(stored: list[JSON], update: JSON) -> list[JSON]:
    """Apply ``update`` to the primary organization (or the first) and keep the rest.

    updatePersonFields=organizations replaces the whole list, so every
    existing entry must be sent back.
    """
    entries = [{k: v for k, v in e.items() if k not in _SERVER_FIELDS} for e in stored]
    target = next((i for i, e in enumerate(stored) if (e.get("metadata") or {}).get("primary")), 0)
    entries[target] = {**entries[target], **update}
    return entries


def contact_fields(
    *,
    given_name: str | None,
    family_name: str | None,
    emails: list[str] | None,
    phones: list[str] | None,
    organization: str | None,
    job_title: str | None,
    notes: str | None,
) -> tuple[JSON, list[str]]:
    """Return the Person body for the given values and the personFields it touches."""
    body: JSON = {}
    fields: list[str] = []
    if given_name is not None or family_name is not None:
        name: JSON = {}
        if given_name is not None:
            name["givenName"] = given_name
        if family_name is not None:
            name["familyName"] = family_name
        body["names"] = [name]
        fields.append("names")
    if emails is not None:
        body["emailAddresses"] = [{"value": e} for e in emails]
        fields.append("emailAddresses")
    if phones is not None:
        body["phoneNumbers"] = [{"value": p} for p in phones]
        fields.append("phoneNumbers")
    if organization is not None or job_title is not None:
        org: JSON = {}
        if organization is not None:
            org["name"] = organization
        if job_title is not None:
            org["title"] = job_title
        body["organizations"] = [org]
        fields.append("organizations")
    if notes is not None:
        body["biographies"] = [{"value": notes, "contentType": "TEXT_PLAIN"}]
        fields.append("biographies")
    return body, fields


class PeopleAdapter:
    spec = SPEC

    def register(self, registry: ToolRegistry, ctx: ServiceContext) -> None:
        # Google asks for one empty-query "warmup" search before the first real
        # search so the server-side cache is fresh.
        warmed: set[str] = set()

        def svc() -> Any:
            return ctx.client("people", "v1")

        def search(kind: str, query: str, max_results: int) -> JSON:
            size = max(1, min(int(max_results), MAX_SEARCH_RESULTS))
            if kind == "contacts":
                resource, mask = svc().people(), LIST_FIELDS
                method = resource.searchContacts
            else:
                resource, mask = svc().otherContacts(), OTHER_CONTACT_FIELDS
                method = resource.search
            if kind not in warmed:
                ctx.execute(method(query="", readMask=mask, pageSize=1))
                warmed.add(kind)
            resp = ctx.execute(method(query=query, readMask=mask, pageSize=size))
            return {"people": [shape_person(r.get("person", {})) for r in resp.get("results", [])]}

        @registry.tool("people_search_contacts")
        def search_contacts(query: str, max_results: int = 10) -> JSON:
            """Search the user's contacts by name, email, phone, or organization (prefix match)."""
            return search("contacts", query, max_results)

        @registry.tool("people_get_person")
        def get_person(resource_name: str = "people/me") -> JSON:
            """Get one contact by resource name (for example 'people/c123'), or 'people/me'."""
            resp = ctx.execute(
                svc()
                .people()
                .get(
                    resourceName=_resource(resource_name, contact=False), personFields=DETAIL_FIELDS
                )
            )
            return shape_person(resp)

        @registry.tool("people_list_connections")
        def list_connections(
            max_results: int = 25,
            page_token: str | None = None,
            sort_order: str = "LAST_MODIFIED_DESCENDING",
        ) -> JSON:
            """List the user's saved contacts."""
            if sort_order not in SORT_ORDERS:
                raise GoogleMCPError(f"sort_order must be one of {sorted(SORT_ORDERS)}")
            resp = ctx.execute(
                svc()
                .people()
                .connections()
                .list(
                    resourceName="people/me",
                    personFields=LIST_FIELDS,
                    pageSize=clamp_page_size(max_results),
                    pageToken=page_token,
                    sortOrder=sort_order,
                )
            )
            return {
                "people": [shape_person(p) for p in resp.get("connections", [])],
                "next_page_token": resp.get("nextPageToken"),
                "total_items": resp.get("totalItems"),
            }

        @registry.tool("people_list_other_contacts")
        def list_other_contacts(max_results: int = 25, page_token: str | None = None) -> JSON:
            """List 'Other contacts': addresses Gmail saved automatically from conversations."""
            resp = ctx.execute(
                svc()
                .otherContacts()
                .list(
                    readMask=OTHER_CONTACT_FIELDS,
                    pageSize=clamp_page_size(max_results),
                    pageToken=page_token,
                )
            )
            return {
                "people": [shape_person(p) for p in resp.get("otherContacts", [])],
                "next_page_token": resp.get("nextPageToken"),
            }

        @registry.tool("people_search_other_contacts")
        def search_other_contacts(query: str, max_results: int = 10) -> JSON:
            """Search 'Other contacts' (auto-saved Gmail correspondents) by name, email, phone."""
            return search("other", query, max_results)

        @registry.tool("people_create_contact", write=True)
        def create_contact(
            given_name: str | None = None,
            family_name: str | None = None,
            emails: list[str] | None = None,
            phones: list[str] | None = None,
            organization: str | None = None,
            job_title: str | None = None,
            notes: str | None = None,
        ) -> JSON:
            """Create a contact. Provide at least a name, email, or phone."""
            body, _ = contact_fields(
                given_name=given_name,
                family_name=family_name,
                emails=emails,
                phones=phones,
                organization=organization,
                job_title=job_title,
                notes=notes,
            )
            if not any(k in body for k in ("names", "emailAddresses", "phoneNumbers")):
                raise GoogleMCPError("provide at least a name, email, or phone")
            resp = ctx.execute(svc().people().createContact(body=body, personFields=DETAIL_FIELDS))
            return shape_person(resp)

        @registry.tool("people_update_contact", write=True, idempotent=True)
        def update_contact(
            resource_name: str,
            given_name: str | None = None,
            family_name: str | None = None,
            emails: list[str] | None = None,
            phones: list[str] | None = None,
            organization: str | None = None,
            job_title: str | None = None,
            notes: str | None = None,
        ) -> JSON:
            """Update a contact. Only the fields you pass change; emails and phones replace the
            existing lists. Unchanged name parts are preserved. organization and job_title
            change the primary organization; other organizations are kept.
            """
            name = _resource(resource_name, contact=True)
            body, fields = contact_fields(
                given_name=given_name,
                family_name=family_name,
                emails=emails,
                phones=phones,
                organization=organization,
                job_title=job_title,
                notes=notes,
            )
            if not fields:
                raise GoogleMCPError("nothing to update")
            client = svc()
            # updateContact returns 400 unless person.metadata.sources names the
            # CONTACT source with its current etag, and names/organizations are
            # replaced wholesale, so read the latest person and merge into it.
            current = ctx.execute(
                client.people().get(resourceName=name, personFields=",".join(["metadata", *fields]))
            )
            sources = [
                pick(src, ("type", "id", "etag"))
                for src in (current.get("metadata", {}) or {}).get("sources", [])
                if src.get("type") == "CONTACT"
            ]
            if not sources:
                raise GoogleMCPError(f"{name} has no contact source and cannot be updated")
            if "names" in body and current.get("names"):
                body["names"] = [merge_name(current["names"][0], body["names"][0])]
            if "organizations" in body and current.get("organizations"):
                body["organizations"] = merge_organizations(
                    current["organizations"], body["organizations"][0]
                )
            body["etag"] = current.get("etag")
            body["metadata"] = {"sources": sources}
            resp = ctx.execute(
                client.people().updateContact(
                    resourceName=name,
                    updatePersonFields=",".join(fields),
                    personFields=DETAIL_FIELDS,
                    body=body,
                )
            )
            return shape_person(resp)

        @registry.tool("people_delete_contact", write=True, destructive=True, idempotent=True)
        def delete_contact(resource_name: str) -> JSON:
            """Delete a contact permanently."""
            name = _resource(resource_name, contact=True)
            ctx.execute(svc().people().deleteContact(resourceName=name))
            return {"deleted": True, "resource_name": name}


ADAPTER = PeopleAdapter()
