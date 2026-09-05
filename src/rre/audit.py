"""Append-only audit trail.

Every decision the agent makes lands here as one JSON object per line, whether
it resulted in an action, a block, or a substitution. Blocks matter as much as
actions: "we did not message this person, because they had opted out" is the
record that answers a complaint, and it is exactly the record most recovery
tooling never writes because nothing happened.

Each entry carries what a reviewer needs to reconstruct the decision without the
code in front of them:

* what the agent believed (root cause, confidence, and the reasoning text)
* which reasoner produced that belief -- llm or deterministic fallback
* what it proposed, and what it was actually allowed to do
* every policy rule that fired, by name
* how to undo it

The rollback field is not decoration. A scheduled retry can be cancelled up to
its scheduled time; a sent message cannot be unsent, and saying so plainly in
the log is more useful than implying everything is reversible.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .domain import Decision, Intervention


def rollback_for(intervention: Intervention, scheduled_for: datetime | None) -> str:
    """State honestly whether this action can be taken back."""
    match intervention:
        case Intervention.RETRY_SCHEDULED:
            when = scheduled_for.isoformat() if scheduled_for else "the scheduled time"
            return f"cancellable until {when}: drop the queued retry job"
        case Intervention.RETRY_NOW:
            return (
                "not reversible: the attempt hits the issuer immediately. "
                "A successful charge is refundable through the normal path"
            )
        case Intervention.SWITCH_RAIL:
            return "reversible before capture: restore the original rail preference"
        case Intervention.NUDGE_CUSTOMER | Intervention.REQUEST_NEW_INSTRUMENT:
            return (
                "NOT reversible: a sent message cannot be unsent. This is why "
                "the contact caps are enforced before sending, not after"
            )
        case Intervention.ESCALATE_HUMAN:
            return "reversible: close the ops ticket, no external side effect"
        case Intervention.STOP:
            return "no action taken, nothing to reverse"
    return "unknown"


@dataclass
class AuditLog:
    """Append-only in behaviour: entries are never edited or removed."""

    path: Path | None = None
    entries: list[dict[str, Any]] = field(default_factory=list)

    def record(self, decision: Decision, *, extra: dict[str, Any] | None = None) -> None:
        v = decision.verdict
        entry: dict[str, Any] = {
            "ts": decision.decided_at.isoformat(),
            "case_id": decision.case_id,
            "belief": {
                "root_cause": str(decision.diagnosis.root_cause),
                "confidence": round(decision.diagnosis.confidence, 3),
                "reasoning": decision.diagnosis.reasoning,
                "reasoner": decision.diagnosis.source,
            },
            "proposed": str(decision.proposed),
            "final": str(decision.final),
            "changed_by_policy": decision.proposed != decision.final,
            "policy": {
                "allowed": v.allowed,
                "rules_fired": list(v.blocked_by),
                "notes": list(v.notes),
            },
            "scheduled_for": (
                decision.scheduled_for.isoformat() if decision.scheduled_for else None
            ),
            "channel": str(decision.channel) if decision.channel else None,
            "message_preview": (
                decision.message[:120] if decision.message else None
            ),
            "rollback": decision.rollback,
        }
        if extra:
            entry.update(extra)
        self.entries.append(entry)

    def flush(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("w", encoding="utf-8") as fh:
            for entry in self.entries:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")

    # -- read-side helpers, used by the report ---------------------------

    def blocked_entries(self) -> list[dict[str, Any]]:
        return [e for e in self.entries if not e["policy"]["allowed"]]

    def substituted_entries(self) -> list[dict[str, Any]]:
        return [e for e in self.entries if e["changed_by_policy"]]

    def by_rule(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for e in self.entries:
            for rule in e["policy"]["rules_fired"]:
                counts[rule] = counts.get(rule, 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: -kv[1]))
