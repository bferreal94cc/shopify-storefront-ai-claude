"""Safety primitives: secrets, webhook authenticity, idempotency, redaction.

Scope, stated plainly. This module does not make the system unhackable. It
closes the specific holes an automated storefront actually gets hurt by:

* **Forged webhooks.** A fake "order paid" callback is a direct route to making
  the system spend real money on a real purchase order. Every inbound webhook is
  HMAC-verified with a constant-time compare before it is believed.
* **Credentials in the repo.** :class:`SecretRef` names a secret; it never holds
  one. Resolution happens at the edge, from the environment or a vault.
* **Duplicate side effects.** Marketplaces retry webhooks, and a retried
  "order created" must not become a second purchase order.
  :class:`IdempotencyGuard` makes every outbound effect exactly-once.
* **PII in permanent records.** The audit chain is immutable by design, so
  anything written there is written forever. :func:`redact` strips emails,
  phone numbers, card-shaped digits and street addresses on the way in.

The structural protection that matters most is not code at all: card data never
enters this system. Shopify, Amazon, eBay and the payment processor hold it,
which keeps the deployment out of PCI scope entirely.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Iterable

# -- redaction -------------------------------------------------------------

REDACTED = "[redacted]"

_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("email", re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")),
    # 13-19 digits, optionally split by spaces or hyphens: card-shaped.
    ("card", re.compile(r"\b(?:\d[ -]?){13,19}\b")),
    ("phone", re.compile(r"\+?\d[\d\s().-]{8,}\d")),
)

_SENSITIVE_KEYS = frozenset(
    {
        "address", "line1", "line2", "street", "email", "phone", "name",
        "customer_name", "ship_to", "token", "secret", "password", "api_key",
        "access_token", "refresh_token", "authorization",
    }
)


def redact_text(value: str) -> str:
    """Mask anything in ``value`` that looks like personal or secret data."""
    for _label, pattern in _PATTERNS:
        value = pattern.sub(REDACTED, value)
    return value


def redact(payload: Any) -> Any:
    """Recursively redact a structure bound for a log or the audit chain.

    Keys whose *name* marks them sensitive keep the key and lose the value to
    ``REDACTED``; free text is pattern-scrubbed. Numbers and booleans pass
    through -- an order total is not PII and the audit trail is useless without
    it. The key survives on purpose: an audit reader needs to see that a token
    was present without being shown it.
    """
    if isinstance(payload, dict):
        clean: dict[str, Any] = {}
        for key, value in payload.items():
            if str(key).lower() in _SENSITIVE_KEYS:
                clean[key] = REDACTED
            else:
                clean[key] = redact(value)
        return clean
    if isinstance(payload, (list, tuple)):
        rebuilt = [redact(item) for item in payload]
        return type(payload)(rebuilt) if isinstance(payload, tuple) else rebuilt
    if isinstance(payload, str):
        return redact_text(payload)
    return payload


# -- secrets ---------------------------------------------------------------


@dataclass(frozen=True)
class SecretRef:
    """A *name* for a credential. Never the credential itself.

    Connectors hold one of these. The value is fetched at the moment of use and
    is never stored on an object, written to the audit log, or serialised into
    run state.
    """

    name: str
    required: bool = True

    def __str__(self) -> str:  # pragma: no cover - defensive, keeps secrets out of logs
        return f"SecretRef({self.name})"


class SecretResolver:
    """Resolves :class:`SecretRef` names to values.

    The default reads the process environment. A deployment swaps in a vault
    client by subclassing and overriding :meth:`lookup`.
    """

    def lookup(self, name: str) -> str | None:
        return os.environ.get(name)

    def resolve(self, ref: SecretRef) -> str:
        value = self.lookup(ref.name)
        if not value:
            if ref.required:
                raise MissingSecret(
                    f"{ref.name} is not set. Export it or supply a vault-backed resolver; "
                    "credentials must never be committed."
                )
            return ""
        return value

    def has(self, ref: SecretRef) -> bool:
        return bool(self.lookup(ref.name))


class MissingSecret(RuntimeError):
    """Raised when a required credential is absent at the point of use."""


class StaticResolver(SecretResolver):
    """In-memory resolver for tests. Never use in a deployment."""

    def __init__(self, values: dict[str, str] | None = None) -> None:
        self._values = dict(values or {})

    def lookup(self, name: str) -> str | None:
        return self._values.get(name)


# -- webhook authenticity --------------------------------------------------


def sign_payload(secret: str, body: bytes, encoding: str = "base64") -> str:
    """Compute the HMAC-SHA256 signature a marketplace would send.

    ``base64`` matches Shopify's ``X-Shopify-Hmac-Sha256``; ``hex`` matches the
    scheme most other platforms use.
    """
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).digest()
    if encoding == "hex":
        return digest.hex()
    if encoding == "base64":
        return base64.b64encode(digest).decode("ascii")
    raise ValueError(f"unsupported signature encoding {encoding!r}")


def verify_webhook(
    secret: str, body: bytes, signature: str, encoding: str = "base64"
) -> bool:
    """Whether ``signature`` authenticates ``body``.

    Compared with :func:`hmac.compare_digest` so a timing side channel cannot be
    used to forge a signature byte by byte. An empty secret or signature is
    always a failure -- never a pass-through.
    """
    if not secret or not signature:
        return False
    try:
        expected = sign_payload(secret, body, encoding)
    except ValueError:
        return False
    return hmac.compare_digest(expected, signature)


def session_token() -> str:
    """A URL-safe token for the local dashboard session."""
    return secrets.token_urlsafe(32)


def constant_time_equals(left: str, right: str) -> bool:
    return hmac.compare_digest(left, right)


# -- idempotency -----------------------------------------------------------


def idempotency_key(*parts: Any) -> str:
    """A stable key for an outbound side effect.

    Same inputs, same key, forever -- so a retried webhook maps onto the effect
    that already happened rather than a new one.
    """
    canonical = "|".join(str(p) for p in parts)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


@dataclass
class IdempotencyGuard:
    """Remembers which side effects have already run.

    ``claim`` returns True exactly once per key. Results are cached so a repeat
    caller can be handed the original outcome instead of doing the work twice.
    """

    _seen: dict[str, Any] = field(default_factory=dict)

    def claim(self, key: str) -> bool:
        """Reserve ``key``. True if this caller owns the effect."""
        if key in self._seen:
            return False
        self._seen[key] = None
        return True

    def record(self, key: str, result: Any) -> None:
        self._seen[key] = result

    def result_for(self, key: str) -> Any:
        return self._seen.get(key)

    def seen(self, key: str) -> bool:
        return key in self._seen

    def __len__(self) -> int:
        return len(self._seen)


# -- rate limiting ---------------------------------------------------------


@dataclass
class RateLimiter:
    """Token bucket, one per connector.

    Marketplaces throttle aggressively and answer a burst with a temporary ban,
    which looks exactly like an outage. Pacing our own calls is cheaper than
    handling the ban.
    """

    capacity: int = 10
    refill_per_second: float = 2.0
    _tokens: float = field(default=0.0, init=False)
    _last: float = field(default=0.0, init=False)

    def __post_init__(self) -> None:
        if self.capacity < 1:
            raise ValueError(f"capacity must be at least 1, got {self.capacity}")
        if self.refill_per_second <= 0:
            raise ValueError(f"refill_per_second must be positive, got {self.refill_per_second}")
        self._tokens = float(self.capacity)
        self._last = time.monotonic()

    def _refill(self, now: float) -> None:
        elapsed = max(0.0, now - self._last)
        self._tokens = min(self.capacity, self._tokens + elapsed * self.refill_per_second)
        self._last = now

    def allow(self, now: float | None = None) -> bool:
        """Consume one token. False when the bucket is empty."""
        self._refill(time.monotonic() if now is None else now)
        if self._tokens >= 1.0:
            self._tokens -= 1.0
            return True
        return False

    def wait_time(self, now: float | None = None) -> float:
        """Seconds until the next token is available."""
        self._refill(time.monotonic() if now is None else now)
        if self._tokens >= 1.0:
            return 0.0
        return (1.0 - self._tokens) / self.refill_per_second


def scrub_headers(headers: Iterable[tuple[str, str]]) -> list[tuple[str, str]]:
    """Redact authorisation headers before anything logs them."""
    out: list[tuple[str, str]] = []
    for key, value in headers:
        out.append((key, REDACTED if key.lower() in _SENSITIVE_KEYS else value))
    return out
