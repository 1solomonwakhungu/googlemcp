"""Scrub URLs and credential-looking query values from upstream error text.

Google error messages sometimes embed request URLs, console links, or
query strings. Tool errors are shown to the model and the user, so any such
text is replaced with ``<url>`` (or ``<redacted>`` for a lone credential
parameter) before it reaches the tool layer. Status codes and reason codes
are kept by callers; only free text passes through here.

The text is percent-decoded first (a bounded number of rounds) and redacted
in decoded form, so encoded URLs and parameters cannot slip through; a final
sweep removes anything still encoded after the last round. This is a
best-effort filter for error text, not a general-purpose sanitizer.
"""

from __future__ import annotations

import re
from urllib.parse import unquote

URL_PLACEHOLDER = "<url>"
VALUE_PLACEHOLDER = "<redacted>"
MAX_DECODE_ROUNDS = 3

# Parameter names (not values) whose values are scrubbed. Longest first so the
# regex alternation prefers access_token over token.
_CREDENTIAL_PARAMS = (
    "access_token|refresh_token|id_token|api_key|client_secret|password|signature|"
    "token|key|code|sig"
)
_STOP = r"[^\s'\"<>]"
# After "=", a quoted value belongs to the URL: ...?access_token='abc'.
_QUOTED_TAIL = rf"(?:(?<==)(?:'[^'\n]*'|\"[^\"\n]*\"){_STOP}*)*"
# A parameter value: quoted (either style) or a bare run.
_VALUE = r"(\"[^\"\n]*\"|'[^'\n]*'|[^\s&'\"<>,;)\]}]+)"

# Any scheme://... run, in any letter case.
_SCHEME_URL = re.compile(rf"\b[a-z][a-z0-9+.-]*://{_STOP}+{_QUOTED_TAIL}", re.IGNORECASE)
# A host (label.tld or IPv4) followed by a path or query, with no scheme.
_HOST_PATH = re.compile(
    rf"\b(?:(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{{2,63}}"
    rf"|(?:\d{{1,3}}\.){{3}}\d{{1,3}})(?::\d{{1,5}})?[/?]{_STOP}*{_QUOTED_TAIL}",
    re.IGNORECASE,
)
# Credential parameters: name=value, name = 'value', name="value".
_PARAM_EQUALS = re.compile(rf"\b({_CREDENTIAL_PARAMS})(\s*=\s*){_VALUE}", re.IGNORECASE)
# JSON or dict form with a quoted name: "name": "value", 'name': 'value'.
_PARAM_QUOTED_NAME = re.compile(rf"([\"'])({_CREDENTIAL_PARAMS})\1(\s*:\s*){_VALUE}", re.IGNORECASE)
# Bare name with a quoted value: name: "value".
_PARAM_COLON_QUOTED = re.compile(
    rf"\b({_CREDENTIAL_PARAMS})(\s*:\s*)(\"[^\"\n]*\"|'[^'\n]*')", re.IGNORECASE
)
# Leftovers still percent-encoded after the last decode round.
_ENCODED_URL = re.compile(rf"\b[a-z][a-z0-9+.-]*%(?:25)*3a%(?:25)*2f{_STOP}*", re.IGNORECASE)
_ENCODED_PARAM = re.compile(rf"\b({_CREDENTIAL_PARAMS})%(?:25)*3d[^\s&'\"<>]*", re.IGNORECASE)
# Sentence punctuation that usually follows a URL rather than belonging to it.
_TRAILING = ".,;:!?)]}"


def _replace_url(match: re.Match[str]) -> str:
    text = match.group(0)
    stripped = text.rstrip(_TRAILING)
    return URL_PLACEHOLDER + text[len(stripped) :]


def _decode(text: str) -> str:
    for _ in range(MAX_DECODE_ROUNDS):
        decoded = unquote(text)
        if decoded == text:
            break
        text = decoded
    return text


def _masked(value: str) -> str:
    """Keep the quoting style of a value while hiding its content."""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        return f"{value[0]}{VALUE_PLACEHOLDER}{value[0]}"
    return VALUE_PLACEHOLDER


def redact(text: str) -> str:
    """Return ``text`` decoded, with URLs and credential parameters replaced."""
    text = _decode(text)
    text = _SCHEME_URL.sub(_replace_url, text)
    text = _ENCODED_URL.sub(_replace_url, text)
    text = _HOST_PATH.sub(_replace_url, text)
    text = _PARAM_EQUALS.sub(lambda m: f"{m[1]}{m[2]}{_masked(m[3])}", text)
    text = _PARAM_QUOTED_NAME.sub(lambda m: f"{m[1]}{m[2]}{m[1]}{m[3]}{_masked(m[4])}", text)
    text = _PARAM_COLON_QUOTED.sub(lambda m: f"{m[1]}{m[2]}{_masked(m[3])}", text)
    return _ENCODED_PARAM.sub(lambda m: f"{m[1]}={VALUE_PLACEHOLDER}", text)
