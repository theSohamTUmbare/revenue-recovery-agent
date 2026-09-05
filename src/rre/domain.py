"""Core domain types.

Design notes that matter for review:

* **Money is integer paise, everywhere.** No float ever touches a rupee value.
  Formatting happens only at the edge, in ``fmt_inr``.
* **Root cause is inferred, never handed over.** The generator stamps a
  ``true_root_cause`` on each case for scoring only; the agent receives the
  observable evidence (``Signal``) and must reason its way there. Nothing in the
  agent pipeline may read ``true_root_cause`` -- ``tests/test_no_leak.py``
  enforces that mechanically.
* **``Intervention.STOP`` is a first-class outcome.** A recovery agent that
  cannot decide to leave someone alone is not a recovery agent, it is a spam
  cannon.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


def fmt_inr(paise: int) -> str:
    """Format integer paise using Indian digit grouping."""
    neg = paise < 0
    rupees, p = divmod(abs(paise), 100)
    s = str(rupees)
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        parts: list[str] = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        s = ",".join(parts) + "," + tail
    return f"{'-' if neg else ''}Rs {s}.{p:02d}"


class Rail(enum.StrEnum):
    """Payment instrument / rail."""

    CARD = "card"
    UPI = "upi"
    NETBANKING = "netbanking"
    WALLET = "wallet"
    EMANDATE = "emandate"
    UPI_AUTOPAY = "upi_autopay"


class FailureCode(enum.StrEnum):
    """Observable gateway failure codes.

    These mirror the shape of Razorpay's ``error.reason`` values. They are the
    *symptom*. Several map to more than one root cause, which is exactly why
    this problem needs judgement rather than a lookup table.
    """

    INSUFFICIENT_FUNDS = "insufficient_funds"
    ISSUER_DOWN = "issuer_down"
    GATEWAY_TIMEOUT = "gateway_timeout"
    NETWORK_ERROR = "network_error"
    CARD_EXPIRED = "card_expired"
    INCORRECT_CVV = "incorrect_cvv"
    MANDATE_REVOKED = "payment_mandate_revoked"
    MANDATE_EXPIRED = "mandate_expired"
    AUTHENTICATION_FAILED = "authentication_failed"
    OTP_TIMEOUT = "otp_timeout"
    DO_NOT_HONOUR = "do_not_honour"
    RISK_DECLINED = "risk_declined"
    LIMIT_EXCEEDED = "limit_exceeded"
    ABANDONED = "checkout_abandoned"
    INVOICE_OVERDUE = "invoice_overdue"


class RootCause(enum.StrEnum):
    """What is *actually* wrong. Inferred by the agent, scored against truth."""

    TRANSIENT_ISSUER_OUTAGE = "transient_issuer_outage"
    RAIL_DEGRADATION = "rail_degradation"
    INSUFFICIENT_FUNDS = "insufficient_funds"
    STALE_INSTRUMENT = "stale_instrument"
    AUTH_FRICTION = "auth_friction"
    HARD_DECLINE = "hard_decline"
    CUSTOMER_INTENT_LOSS = "customer_intent_loss"
    RECEIVABLE_OVERDUE = "receivable_overdue"


class Intervention(enum.StrEnum):
    """What the agent decides to do about it."""

    RETRY_NOW = "retry_now"
    RETRY_SCHEDULED = "retry_scheduled"
    SWITCH_RAIL = "switch_rail"
    REQUEST_NEW_INSTRUMENT = "request_new_instrument"
    NUDGE_CUSTOMER = "nudge_customer"
    ESCALATE_HUMAN = "escalate_human"
    STOP = "stop"


#: Interventions that put a message in front of a human being. The policy layer
#: gates these hardest.
CONTACT_INTERVENTIONS: frozenset[Intervention] = frozenset(
    {
        Intervention.REQUEST_NEW_INSTRUMENT,
        Intervention.NUDGE_CUSTOMER,
    }
)

#: Interventions that move money without asking anyone.
SILENT_INTERVENTIONS: frozenset[Intervention] = frozenset(
    {
        Intervention.RETRY_NOW,
        Intervention.RETRY_SCHEDULED,
        Intervention.SWITCH_RAIL,
    }
)


class Channel(enum.StrEnum):
    EMAIL = "email"
    SMS = "sms"
    WHATSAPP = "whatsapp"
    VOICE = "voice"


@dataclass(frozen=True, slots=True)
class Customer:
    customer_id: str
    name: str
    #: Channels this customer actually consented to. The policy layer will not
    #: use any channel outside this set, whatever the agent asks for.
    consented_channels: frozenset[Channel]
    opted_out: bool
    timezone_offset_min: int = 330  # IST
    #: Set when the customer has told us they will pay by a date. Contacting
    #: them before it is a policy violation, not a judgement call.
    promise_to_pay_date: datetime | None = None
    dispute_open: bool = False
    #: Contacts already made this week, before this batch runs.
    contacts_this_week: int = 0


@dataclass(frozen=True, slots=True)
class Signal:
    """Everything the agent is allowed to see about one at-risk payment."""

    case_id: str
    customer_id: str
    amount_paise: int
    rail: Rail
    failure_code: FailureCode
    occurred_at: datetime
    attempt_number: int
    #: Prior failure codes on this same case, oldest first.
    prior_failures: tuple[FailureCode, ...] = ()
    #: Fleet-wide context, so the agent can tell "this bank is down right now"
    #: apart from "this one customer has no money".
    issuer_bank: str = "UNKNOWN"
    issuer_failure_rate_1h: float = 0.0
    rail_failure_rate_1h: float = 0.0
    #: Days overdue, receivables only.
    days_overdue: int = 0
    #: Free text the customer sent us, if any. Genuinely unstructured.
    customer_reply: str | None = None
    subscription: bool = False

    def to_evidence(self) -> dict[str, Any]:
        """The exact payload handed to the reasoner. Contains no truth fields."""
        return {
            "case_id": self.case_id,
            "amount": fmt_inr(self.amount_paise),
            "rail": str(self.rail),
            "failure_code": str(self.failure_code),
            "attempt_number": self.attempt_number,
            "prior_failures": [str(f) for f in self.prior_failures],
            "issuer_bank": self.issuer_bank,
            "issuer_failure_rate_1h": round(self.issuer_failure_rate_1h, 3),
            "rail_failure_rate_1h": round(self.rail_failure_rate_1h, 3),
            "days_overdue": self.days_overdue,
            "customer_reply": self.customer_reply,
            "is_subscription": self.subscription,
            "occurred_at": self.occurred_at.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class Diagnosis:
    root_cause: RootCause
    confidence: float
    reasoning: str
    #: "llm" or "deterministic-fallback" -- recorded so a reader can always tell
    #: which path produced any given number.
    source: str
    #: Parsed out of a free-text customer reply when one is present.
    extracted_promise_date: datetime | None = None


@dataclass(frozen=True, slots=True)
class PolicyVerdict:
    allowed: bool
    #: Rule identifiers that fired, e.g. ("quiet_hours", "contact_cap").
    blocked_by: tuple[str, ...] = ()
    #: Human-readable explanation, one line per rule that fired.
    notes: tuple[str, ...] = ()
    #: Policy may downgrade rather than block outright -- a contact during quiet
    #: hours becomes a contact scheduled after 09:00.
    substituted: Intervention | None = None
    scheduled_for: datetime | None = None
    channel: Channel | None = None


@dataclass(frozen=True, slots=True)
class Decision:
    case_id: str
    proposed: Intervention
    final: Intervention
    diagnosis: Diagnosis
    verdict: PolicyVerdict
    decided_at: datetime
    scheduled_for: datetime | None = None
    channel: Channel | None = None
    message: str | None = None
    #: How we would undo this if it turns out to be wrong.
    rollback: str = "none required"


@dataclass(frozen=True, slots=True)
class Outcome:
    case_id: str
    recovered: bool
    recovered_paise: int
    #: Whether a human was contacted at all.
    contacted: bool
    contacts_used: int
    #: True when we touched someone we should not have. Zero for the policy-gated
    #: agent by construction; the naive baselines rack these up.
    violation: bool = False
    violation_kinds: tuple[str, ...] = ()
    notes: str = ""


@dataclass(slots=True)
class Case:
    """A signal plus its ground truth. Only the harness sees the truth half."""

    signal: Signal
    customer: Customer
    true_root_cause: RootCause
    #: Whether this money was ever recoverable at all, by anyone. Some of it
    #: simply is not, and a system claiming otherwise is lying.
    recoverable: bool
    decisions: list[Decision] = field(default_factory=list)
    outcome: Outcome | None = None


@dataclass(frozen=True, slots=True)
class PolicyConfig:
    """Every guardrail, in one auditable place. Pure data, never LLM input."""

    max_contacts_per_case: int = 2
    max_contacts_per_customer_per_week: int = 3
    min_hours_between_contacts: int = 48
    quiet_hours_start: int = 21  # 21:00 local
    quiet_hours_end: int = 9  # 09:00 local
    max_retries_per_case: int = 3
    #: Root causes where retrying is known-futile. Retrying these burns issuer
    #: goodwill and inflates the merchant's decline rate for nothing.
    no_retry_causes: frozenset[RootCause] = frozenset(
        {
            RootCause.STALE_INSTRUMENT,
            RootCause.HARD_DECLINE,
        }
    )
    #: Below this, chasing costs more than the money is worth.
    min_amount_to_pursue_paise: int = 5_000  # Rs 50
    #: Confidence floor. Under this the agent escalates to a human rather than
    #: acting on a guess.
    min_confidence_to_act: float = 0.55
    promise_to_pay_grace_days: int = 1


def default_policy() -> PolicyConfig:
    return PolicyConfig()
