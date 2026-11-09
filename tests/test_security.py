"""Webhook authenticity, secret handling, idempotency, redaction."""

from __future__ import annotations

import pytest

from storefront_agent.security import (
    REDACTED,
    IdempotencyGuard,
    MissingSecret,
    RateLimiter,
    SecretRef,
    SecretResolver,
    StaticResolver,
    idempotency_key,
    redact,
    redact_text,
    scrub_headers,
    session_token,
    sign_payload,
    verify_webhook,
)

BODY = b'{"id":123,"total":"49.99"}'


class TestWebhookVerification:
    def test_a_correct_signature_verifies(self):
        assert verify_webhook("shh", BODY, sign_payload("shh", BODY))

    def test_hex_encoding_round_trips(self):
        signature = sign_payload("shh", BODY, "hex")
        assert verify_webhook("shh", BODY, signature, "hex")

    def test_a_tampered_body_fails(self):
        signature = sign_payload("shh", BODY)
        assert not verify_webhook("shh", b'{"id":123,"total":"4999.99"}', signature)

    def test_the_wrong_secret_fails(self):
        assert not verify_webhook("other", BODY, sign_payload("shh", BODY))

    def test_an_empty_secret_never_passes(self):
        assert not verify_webhook("", BODY, sign_payload("shh", BODY))

    def test_an_empty_signature_never_passes(self):
        assert not verify_webhook("shh", BODY, "")

    def test_an_unknown_encoding_fails_closed(self):
        assert not verify_webhook("shh", BODY, "anything", "rot13")

    def test_signing_with_an_unknown_encoding_raises(self):
        with pytest.raises(ValueError, match="unsupported"):
            sign_payload("shh", BODY, "rot13")


class TestRedaction:
    def test_emails_and_phones_are_masked(self):
        assert redact_text("mail dana@example.com or 555-123-4567") == f"mail {REDACTED} or {REDACTED}"

    def test_card_shaped_digits_are_masked(self):
        assert REDACTED in redact_text("4111 1111 1111 1111")

    def test_sensitive_keys_are_dropped_by_name(self):
        assert redact({"email": "a@b.com", "api_key": "sk-live-1"}) == {
            "email": REDACTED, "api_key": REDACTED
        }

    def test_numbers_survive_because_the_audit_needs_them(self):
        assert redact({"total_cents": 4999, "ok": True}) == {"total_cents": 4999, "ok": True}

    def test_nested_structures_are_walked(self):
        result = redact({"order": {"notes": ["call dana@example.com"]}})
        assert result["order"]["notes"] == [f"call {REDACTED}"]

    def test_tuples_stay_tuples(self):
        assert isinstance(redact(("dana@example.com",)), tuple)

    def test_authorisation_headers_are_scrubbed(self):
        assert scrub_headers([("Authorization", "Bearer x"), ("Accept", "json")]) == [
            ("Authorization", REDACTED), ("Accept", "json")
        ]


class TestSecrets:
    def test_a_missing_required_secret_raises_with_guidance(self):
        with pytest.raises(MissingSecret, match="never be committed"):
            SecretResolver().resolve(SecretRef("DEFINITELY_NOT_SET_XYZ"))

    def test_an_optional_missing_secret_is_empty(self):
        assert SecretResolver().resolve(SecretRef("NOT_SET_XYZ", required=False)) == ""

    def test_a_static_resolver_serves_test_values(self):
        resolver = StaticResolver({"TOKEN": "abc"})
        assert resolver.resolve(SecretRef("TOKEN")) == "abc"
        assert resolver.has(SecretRef("TOKEN"))

    def test_repr_does_not_leak_the_value(self):
        assert "abc" not in str(SecretRef("TOKEN"))

    def test_session_tokens_are_unique_and_long(self):
        first, second = session_token(), session_token()
        assert first != second and len(first) >= 32


class TestIdempotency:
    def test_a_key_is_stable_for_the_same_inputs(self):
        assert idempotency_key("po", 1) == idempotency_key("po", 1)

    def test_different_inputs_give_different_keys(self):
        assert idempotency_key("po", 1) != idempotency_key("po", 2)

    def test_claim_succeeds_once(self):
        guard = IdempotencyGuard()
        assert guard.claim("k")
        assert not guard.claim("k")

    def test_results_are_cached_for_the_retrying_caller(self):
        guard = IdempotencyGuard()
        guard.claim("k")
        guard.record("k", "shipped")
        assert guard.result_for("k") == "shipped"
        assert guard.seen("k") and len(guard) == 1


class TestRateLimiter:
    def test_a_burst_is_capped_at_capacity(self):
        limiter = RateLimiter(capacity=3, refill_per_second=1.0)
        assert [limiter.allow(now=100.0) for _ in range(4)] == [True, True, True, False]

    def test_tokens_refill_over_time(self):
        limiter = RateLimiter(capacity=2, refill_per_second=1.0)
        limiter.allow(now=100.0)
        limiter.allow(now=100.0)
        assert not limiter.allow(now=100.0)
        assert limiter.allow(now=101.5)

    def test_wait_time_reports_when_the_next_token_lands(self):
        limiter = RateLimiter(capacity=1, refill_per_second=2.0)
        limiter.allow(now=100.0)
        assert limiter.wait_time(now=100.0) == pytest.approx(0.5)

    def test_wait_time_is_zero_when_tokens_remain(self):
        assert RateLimiter(capacity=2).wait_time(now=100.0) == 0.0

    @pytest.mark.parametrize("kwargs", [{"capacity": 0}, {"refill_per_second": 0}])
    def test_nonsense_configuration_raises(self, kwargs):
        with pytest.raises(ValueError):
            RateLimiter(**kwargs)
