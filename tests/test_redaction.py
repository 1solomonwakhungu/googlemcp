from urllib.parse import quote

import pytest

from googlemcp.redaction import VALUE_PLACEHOLDER, redact


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "Permission denied. See https://example.invalid/private?token=synthetic for details.",
            "Permission denied. See <url> for details.",
        ),
        (
            "Enable it by visiting https://console.developers.google.com/apis/api/"
            "tasks.googleapis.com/overview?project=123 then retry.",
            "Enable it by visiting <url> then retry.",
        ),
        ("call tasks.googleapis.com/tasks/v1/lists?key=AIzaFAKE failed", "call <url> failed"),
        ("see (http://a.example/c), then", "see (<url>), then"),
        ("token=abc&x=1 and api_key=zzz", "token=<redacted>&x=1 and api_key=<redacted>"),
        ("access_token=ya29.secret in body", "access_token=<redacted> in body"),
    ],
)
def test_urls_and_credentials_are_redacted(text: str, expected: str) -> None:
    assert redact(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "Requested entity was not found.",
        "Quota exceeded for quota metric 'Queries' of service 'tasks.googleapis.com'.",
        "",
    ],
)
def test_ordinary_messages_are_unchanged(text: str) -> None:
    assert redact(text) == text


def test_type_urls_on_googleapis_are_treated_as_urls() -> None:
    text = "Invalid value at 'task.due' (type.googleapis.com/google.protobuf.Timestamp)"
    assert redact(text) == "Invalid value at 'task.due' (<url>)"


# Regression cases: percent-encoded URL bypasses.

ENCODED_URL = "https%3A%2F%2Fexample.invalid%2Fprivate%3Ftoken%3Dsynthetic"


def _encode(text: str, rounds: int) -> str:
    for _ in range(rounds):
        text = quote(text, safe="")
    return text


def _clean(text: str) -> None:
    out = redact(text)
    lowered = out.lower()
    for leak in ("example.invalid", "synthetic", "://", "%3a", "%2f", "token=s", "private"):
        assert leak not in lowered, (text, out)


def test_percent_encoded_url_is_redacted() -> None:
    assert redact(ENCODED_URL) == "<url>"
    assert redact(f"Denied: {ENCODED_URL}.") == "Denied: <url>."


@pytest.mark.parametrize("rounds", [1, 2, 3, 4, 6])
def test_percent_encoded_urls_at_any_depth(rounds: int) -> None:
    _clean(_encode("https://example.invalid/private?token=synthetic", rounds))


@pytest.mark.parametrize(
    "text",
    [
        "HTTPS://Example.Invalid/Private?token=synthetic",
        "hTtP://example.invalid/private",
        "see example.invalid/private?token=synthetic now",
        "host example.invalid?token=synthetic",
        "sub.example.invalid:8443/private/path",
        "at 10.1.2.3:8443/private.",
    ],
)
def test_scheme_case_and_bare_host_paths(text: str) -> None:
    _clean(text)
    assert "<url>" in redact(text)


@pytest.mark.parametrize(
    "name",
    [
        "token",
        "access_token",
        "refresh_token",
        "id_token",
        "key",
        "api_key",
        "code",
        "client_secret",
        "password",
        "sig",
        "signature",
    ],
)
@pytest.mark.parametrize("form", ["raw", "encoded", "double", "upper"])
def test_credential_params_raw_or_encoded(name: str, form: str) -> None:
    param = {
        "raw": f"{name}=s3cr3tvalue",
        "encoded": f"{name}%3Ds3cr3tvalue",
        "double": f"{name}%253Ds3cr3tvalue",
        "upper": f"{name.upper()}=s3cr3tvalue",
    }[form]
    out = redact(f"failed with {param} in request")
    assert "s3cr3tvalue" not in out
    assert out.lower() == f"failed with {name}={VALUE_PLACEHOLDER} in request".lower()


@pytest.mark.parametrize(
    "text",
    [
        "Request had invalid authentication credentials. Expected OAuth 2 access token.",
        "Invalid value at 'task.due' (google.protobuf.Timestamp)",
        "users/me/settings not found",
        "Version 2.0/3 mismatch",
        "Progress 100% complete",
        "e.g. example.com is fine",
    ],
)
def test_more_ordinary_messages_are_unchanged(text: str) -> None:
    assert redact(text) == text


# Regression cases: quoted and JSON forms.


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Denied https://example.invalid/private?access_token='synthetic'", "Denied <url>"),
        ('Denied https://example.invalid/p?access_token="synthetic" now', "Denied <url> now"),
        ("example.invalid/p?token='synthetic'", "<url>"),
        ('{"access_token": "synthetic", "x": 1}', '{"access_token": "<redacted>", "x": 1}'),
        ("{'api_key': 'synthetic'}", "{'api_key': '<redacted>'}"),
        ('{"password":"synthetic"}', '{"password":"<redacted>"}'),
        (
            "key=\"synthetic\" and password = 'synthetic'",
            "key=\"<redacted>\" and password = '<redacted>'",
        ),
        ('token: "synthetic"', 'token: "<redacted>"'),
        ("client_secret='synthetic'", "client_secret='<redacted>'"),
    ],
)
def test_quoted_and_json_credentials(text: str, expected: str) -> None:
    assert redact(text) == expected
    assert "synthetic" not in redact(text)


@pytest.mark.parametrize(
    "text",
    [
        "Error code: 403",
        "Field 'code' is required",
        'expected key "name"',
        "visit '<url>' now",
    ],
)
def test_colon_and_quote_text_without_values_is_unchanged(text: str) -> None:
    assert redact(text) == text


def test_quoted_url_keeps_its_quotes() -> None:
    assert redact("visit 'https://x.invalid/y' now") == "visit '<url>' now"
    assert redact('see "https://x.invalid/y", then') == 'see "<url>", then'
