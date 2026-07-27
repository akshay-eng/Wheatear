"""Strip credentials out of probed records before anything else touches them.

The inspector's whole job is to pull real records off real tenants, and those
records carry bearer tokens, connection strings and session cookies. Three
things downstream would otherwise leak them: the corpus is written to disk,
sample values are shown to a language model, and both end up in a review
manifest a human may paste somewhere. So redaction runs at the probe boundary,
before a record is stored, hashed, or looked at -- not at each of the three
places it could escape from.

Two rules, because either alone misses obvious cases:

  * **by key name** -- `client_secret`, `Authorization`, `Cookie`. Catches a
    credential whose value looks like nothing in particular.
  * **by value shape** -- JWTs, `Bearer …`, `https://user:pass@host`, long
    unbroken high-entropy strings. Catches a credential stored under a key
    nobody would flag, which is the more common case in an export.

Redaction preserves JSON type. A redacted string stays a string, so the shape
inference that runs afterwards sees the same structure it would have seen --
the corpus stays accurate about what a platform's records look like while
being wrong, deliberately, about what was in them.

This is a reduction in exposure, not a guarantee. A credential in a field
called `notes`, in prose, with no recognisable shape, survives -- which is why
the foundry also never sends whole records to a model, only field paths, types
and truncated examples.
"""

from __future__ import annotations

import re
from typing import Any

# The value a redacted field is replaced with. Recognisable on sight, and
# recognisable to code: `shape._looks_like_enum` refuses to treat these as an
# enum vocabulary, so a redacted field can't smuggle a marker into the corpus
# fingerprint.
MARKER = "<redacted>"


def _marked(what: str) -> str:
    return f"<redacted:{what}>"


# Substrings that make a key name a credential. Matched against the key with
# separators removed, so `client_secret`, `clientSecret` and `client-secret`
# are one rule rather than three.
_SECRET_MARKERS = (
    "secret",
    "password",
    "passwd",
    "pwd",
    "token",
    "cookie",
    "apikey",
    "accesskey",
    "privatekey",
    "credential",
    "authorization",
    "bearer",
    "signature",
    "connectionstring",
    "sessionkey",
    "sharedkey",
    "certificate",
    "passphrase",
)

# Key names that contain a marker substring and are not credentials. "token"
# is the offender: `max_tokens` and `tokenizer` are configuration a mapping
# genuinely needs to see, and redacting them would produce an adapter that
# drops a model's context limit.
_SAFE_MARKERS = (
    "tokenizer",
    "maxtoken",
    "tokenlimit",
    "tokencount",
    "tokenusage",
    "tokenizermodel",
    "signaturealgorithm",
)

_NON_ALNUM = re.compile(r"[^a-z0-9]")

# A JWT: three base64url segments, and the leading `eyJ` that every JSON header
# begins with. Distinctive enough to match on sight with no false positives.
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{4,}")
_BEARER_RE = re.compile(r"\b[Bb]earer\s+[A-Za-z0-9._~+/=-]{12,}")
_URL_CRED_RE = re.compile(r"(?<=://)[^/\s:@]+:[^/\s@]+(?=@)")
# `name=value` pairs from a Cookie header, matched anywhere a cookie blob got
# stored as prose rather than under a key called `cookie`.
_COOKIE_RE = re.compile(r"\b(__Secure-[A-Za-z0-9_-]+|__Host-[A-Za-z0-9_-]+)=[^;\s]+")
# A long unbroken run of base64/hex with both letters and digits. Length and
# the absence of whitespace are what keep prose out: instructions and
# descriptions have spaces, keys do not.
_BLOB_RE = re.compile(
    r"(?<![A-Za-z0-9+/=_-])(?=[A-Za-z0-9+/=_-]*[0-9])(?=[A-Za-z0-9+/=_-]*[A-Za-z])"
    r"[A-Za-z0-9+/=_-]{40,}(?![A-Za-z0-9+/=_-])"
)

# Depth and size limits: a probe can hand us an arbitrarily nested response,
# and redaction must terminate on it.
MAX_DEPTH = 12


def is_secret_key(key: str) -> bool:
    """Whether a field name alone is enough to call its value a credential."""
    normalized = _NON_ALNUM.sub("", str(key).lower())
    if any(safe in normalized for safe in _SAFE_MARKERS):
        return False
    return any(marker in normalized for marker in _SECRET_MARKERS)


def redact_text(text: str) -> str:
    """Redact credential-shaped substrings inside a string, leaving the rest.

    Substring rather than whole-value replacement because a credential is
    often embedded in something we want to keep: an endpoint with a SAS token
    in its query string is still evidence of an endpoint.
    """
    if not text:
        return text
    redacted = _JWT_RE.sub(_marked("jwt"), text)
    redacted = _BEARER_RE.sub(f"Bearer {_marked('token')}", redacted)
    redacted = _URL_CRED_RE.sub(_marked("userinfo"), redacted)
    redacted = _COOKIE_RE.sub(lambda m: f"{m.group(1)}={_marked('cookie')}", redacted)
    if " " not in redacted:
        redacted = _BLOB_RE.sub(_marked("blob"), redacted)
    return redacted


def _redact_value(key: str | None, value: Any, depth: int) -> Any:
    if depth > MAX_DEPTH:
        return MARKER

    if isinstance(value, dict):
        return {k: _redact_value(str(k), v, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_value(key, item, depth + 1) for item in value]

    if key is not None and is_secret_key(key):
        # Type-preserving: a secret that was a string stays a string, so the
        # inferred shape is unchanged. A non-string under a secret name is
        # dropped to None rather than stringified, for the same reason -- the
        # corpus should not learn that this field holds strings when it doesn't.
        return _marked("secret") if isinstance(value, str) else None

    if isinstance(value, str):
        return redact_text(value)
    return value


def redact(record: Any) -> Any:
    """Return a copy of `record` with credentials removed.

    Pure -- the caller's object is never mutated, because a probe may hand the
    same response to more than one consumer and one of them silently editing
    it would be a genuinely nasty bug to find.
    """
    return _redact_value(None, record, 0)


def redact_all(records: list[dict]) -> list[dict]:
    return [redact(record) for record in records if isinstance(record, dict)]
