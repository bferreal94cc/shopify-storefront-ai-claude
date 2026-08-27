"""The audit chain must detect any edit to history."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime

from storefront_agent.audit import GENESIS_HASH, AuditLog

AT = datetime(2026, 8, 21, 9, 0)


def populated(count: int = 3) -> AuditLog:
    log = AuditLog()
    for index in range(count):
        log.record("actor", f"action_{index}", f"subject_{index}", f"because {index}", at=AT)
    return log


class TestChain:
    def test_the_first_entry_chains_to_genesis(self):
        log = populated(1)
        assert log.entries[0].previous_hash == GENESIS_HASH

    def test_entries_chain_to_their_predecessor(self):
        log = populated(3)
        for previous, current in zip(log.entries, log.entries[1:]):
            assert current.previous_hash == previous.entry_hash

    def test_an_untouched_chain_verifies(self):
        result = populated(5).verify()
        assert result.valid and "5 entries verified" in result.detail

    def test_an_empty_chain_verifies(self):
        assert AuditLog().verify().valid

    def test_editing_an_entry_breaks_the_chain_at_that_point(self):
        log = populated(4)
        log.entries[2] = replace(log.entries[2], reasoning="something else entirely")
        result = log.verify()
        assert not result.valid and result.broken_at == 2
        assert "altered" in result.detail

    def test_deleting_an_entry_is_detected(self):
        log = populated(4)
        del log.entries[1]
        result = log.verify()
        assert not result.valid and result.broken_at == 1

    def test_reordering_is_detected(self):
        log = populated(3)
        log.entries[0], log.entries[1] = log.entries[1], log.entries[0]
        assert not log.verify().valid


class TestRecording:
    def test_metadata_is_redacted_before_it_becomes_permanent(self):
        log = AuditLog()
        entry = log.record("a", "b", "c", "d", metadata={"email": "x@y.com", "total": 99})
        assert entry.metadata["email"] == "[redacted]"
        assert entry.metadata["total"] == 99

    def test_a_redacted_entry_still_verifies(self):
        log = AuditLog()
        log.record("a", "b", "c", "d", metadata={"email": "x@y.com"})
        assert log.verify().valid


class TestQueries:
    def test_filtering_by_subject_and_actor(self):
        log = AuditLog()
        log.record("alice", "x", "order-1", "r", at=AT)
        log.record("bob", "y", "order-2", "r", at=AT)
        assert len(log.entries_for("order-1")) == 1
        assert len(log.entries_by("bob")) == 1

    def test_since_returns_only_later_entries(self):
        log = AuditLog()
        log.record("a", "x", "s", "r", at=datetime(2026, 8, 21, 9, 0))
        log.record("a", "y", "s", "r", at=datetime(2026, 8, 21, 17, 0))
        assert len(log.since(datetime(2026, 8, 21, 12, 0))) == 1

    def test_jsonl_export_is_one_line_per_entry(self):
        export = populated(3).export_jsonl()
        assert len(export.splitlines()) == 3
        assert '"entry_hash"' in export
