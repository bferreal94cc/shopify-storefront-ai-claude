"""Append-only, tamper-evident audit trail.

Every listing, price change, purchase order, payment hold and shipment lands
here with the reasoning that produced it. When a marketplace opens a seller
performance case, or a chargeback needs defending, the question is always *why
did the system do that, and when* -- months after the fact.

Entries are chained by hash: each commits to the one before it, so editing or
deleting history invalidates every entry after the change and :meth:`AuditLog.verify`
reports exactly where the chain broke. This detects tampering; it does not
prevent it. Durable storage and access control are the deployment's job.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

GENESIS_HASH = "0" * 64


@dataclass(frozen=True)
class AuditEntry:
    """One immutable record in the chain."""

    sequence: int
    timestamp: datetime
    actor: str
    action: str
    subject: str
    reasoning: str
    metadata: dict[str, Any]
    previous_hash: str
    entry_hash: str

    def payload(self) -> dict[str, Any]:
        """The exact bytes the hash commits to."""
        return {
            "sequence": self.sequence,
            "timestamp": self.timestamp.isoformat(),
            "actor": self.actor,
            "action": self.action,
            "subject": self.subject,
            "reasoning": self.reasoning,
            "metadata": self.metadata,
            "previous_hash": self.previous_hash,
        }

    def to_json(self) -> str:
        record = self.payload()
        record["entry_hash"] = self.entry_hash
        return json.dumps(record, sort_keys=True, default=str)


def _hash_payload(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass
class VerificationResult:
    """Outcome of re-walking the chain."""

    valid: bool
    broken_at: int | None = None
    detail: str = ""


@dataclass
class AuditLog:
    """An ordered, hash-chained list of decisions."""

    entries: list[AuditEntry] = field(default_factory=list)

    def record(
        self,
        actor: str,
        action: str,
        subject: str,
        reasoning: str,
        metadata: dict[str, Any] | None = None,
        at: datetime | None = None,
    ) -> AuditEntry:
        """Append a decision. Returns the entry that was written.

        Metadata is redacted before it is hashed, so a customer email captured
        by accident never becomes a permanent, immutable record.
        """
        from .security import redact

        previous_hash = self.entries[-1].entry_hash if self.entries else GENESIS_HASH
        safe_metadata = redact(dict(metadata or {}))
        payload = {
            "sequence": len(self.entries),
            "timestamp": (at or datetime.now()).isoformat(),
            "actor": actor,
            "action": action,
            "subject": subject,
            "reasoning": reasoning,
            "metadata": safe_metadata,
            "previous_hash": previous_hash,
        }
        entry = AuditEntry(
            sequence=payload["sequence"],
            timestamp=datetime.fromisoformat(payload["timestamp"]),
            actor=actor,
            action=action,
            subject=subject,
            reasoning=reasoning,
            metadata=safe_metadata,
            previous_hash=previous_hash,
            entry_hash=_hash_payload(payload),
        )
        self.entries.append(entry)
        return entry

    def verify(self) -> VerificationResult:
        """Re-walk the chain and report the first inconsistency."""
        expected_previous = GENESIS_HASH
        for index, entry in enumerate(self.entries):
            if entry.sequence != index:
                return VerificationResult(
                    False, index, f"entry {index} claims sequence {entry.sequence}"
                )
            if entry.previous_hash != expected_previous:
                return VerificationResult(
                    False, index, f"entry {index} does not chain to {index - 1}"
                )
            if _hash_payload(entry.payload()) != entry.entry_hash:
                return VerificationResult(False, index, f"entry {index} contents were altered")
            expected_previous = entry.entry_hash
        return VerificationResult(True, None, f"{len(self.entries)} entries verified")

    def entries_for(self, subject: str) -> list[AuditEntry]:
        return [e for e in self.entries if e.subject == subject]

    def entries_by(self, actor: str) -> list[AuditEntry]:
        return [e for e in self.entries if e.actor == actor]

    def since(self, moment: datetime) -> list[AuditEntry]:
        """Everything recorded after ``moment`` -- the check-in digest's input."""
        return [e for e in self.entries if e.timestamp > moment]

    def export_jsonl(self) -> str:
        """Newline-delimited JSON, the format regulators and SIEMs both accept."""
        return "\n".join(entry.to_json() for entry in self.entries)
