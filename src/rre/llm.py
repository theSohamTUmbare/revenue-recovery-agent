"""The reasoning layer, and the seam it hides behind.

Two implementations of one protocol:

* ``AnthropicReasoner`` -- the real path. Claude reads the evidence for a case,
  infers a root cause, and parses any free-text customer reply.
* ``OfflineReasoner`` -- a deterministic rule engine covering the same
  interface, so a reviewer with no API key still gets a complete, honest run.

**Why there is a fallback at all.** Two reasons, and the second is the real one:

1. A reviewer should be able to ``git clone && make demo`` and see the whole
   pipeline work in thirty seconds without being asked for a key.
2. It is the control arm. Running both paths over the same 400 cases and
   diffing the accuracy is the only way to answer "did the LLM actually earn its
   place here, or would a lookup table have done?" ``make ablation`` runs exactly
   that comparison, and the README publishes the gap. If the gap were nil we
   would have deleted the LLM, which is the point of measuring it.

The reasoner is deliberately kept away from anything consequential. It infers a
cause and proposes an action; it never decides whether a human gets contacted.
That decision belongs to ``policy.py``, which is plain code with no model in the
loop. See the README section "Where we chose not to use AI".
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from .domain import RootCause

# Default model. Overridable with RRE_LLM_MODEL.
DEFAULT_MODEL = "claude-opus-5"

# Effort is deliberately "low". Diagnosis is a bounded classification over
# structured evidence, not open-ended reasoning, and we run it across hundreds
# of cases per batch. Low effort holds accuracy here (see the ablation table in
# the README) at a fraction of the token spend. Raise via RRE_LLM_EFFORT if you
# want to check that claim yourself.
DEFAULT_EFFORT = "low"

SYSTEM_PROMPT = """\
You are a payments failure analyst for an Indian payment gateway. You are given \
the observable evidence from one failed or at-risk payment. Infer the single \
most likely underlying root cause.

The failure code alone is not sufficient and is sometimes actively misleading:

- `do_not_honour` is returned by Indian issuers for BOTH a genuine risk decline \
AND a simple empty account. Use the amount, attempt history and any customer \
reply to break the tie.
- `gateway_timeout` and `network_error` indicate an outage, but you must decide \
WHOSE. A high `issuer_failure_rate_1h` means that specific bank is down right \
now -> transient_issuer_outage. A high `rail_failure_rate_1h` with a normal \
issuer rate means the payment method itself is degraded -> rail_degradation. \
Both near zero means the infrastructure is fine and the problem is specific to \
this one customer.
- `insufficient_funds` reported by a bank that is mid-outage is often the bank \
misreporting, not an empty account. Check the issuer failure rate.
- `incorrect_cvv` can be a dead card (stale_instrument) or a fumbled checkout \
(auth_friction). Repeat attempts with the same code lean stale.

Available root causes:
- transient_issuer_outage: the customer's bank is temporarily down. Will pass later.
- rail_degradation: this payment rail/gateway is degraded. Another rail will work.
- insufficient_funds: the customer genuinely lacks the money right now.
- stale_instrument: expired card, revoked or expired mandate. Retrying is futile.
- auth_friction: the customer failed or abandoned OTP / 3DS. A lower-friction rail helps.
- hard_decline: the issuer refused for a structural or risk reason. Will not pass.
- customer_intent_loss: the customer abandoned checkout. Nothing failed technically.
- receivable_overdue: a B2B invoice past its due date.

If a `customer_reply` is present, read it carefully. It may be in Hindi, English \
or Hinglish. Extract:
- Any commitment to pay by a date, as an ISO date. "5 tarikh ko" means the 5th of \
the coming month; "month end" means the last day of the current month; "Friday" \
means the next Friday. If no date is committed, use null.
- Whether the customer is asking to be left alone, disputing the charge, or \
reporting a broken instrument.

Be honest in `confidence`. If the evidence genuinely does not distinguish between \
two causes, say so with a confidence below 0.55 -- a low confidence routes the \
case to a human instead of to an automated action, which is the correct outcome \
when you do not know. Overconfidence here costs real money.
"""

DIAGNOSIS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "root_cause": {
            "type": "string",
            "enum": [str(c) for c in RootCause],
        },
        "confidence": {
            "type": "number",
            "description": "0.0 to 1.0. Below 0.55 routes to a human.",
        },
        "reasoning": {
            "type": "string",
            "description": "One or two sentences citing the specific evidence used.",
        },
        "promise_to_pay_date": {
            "type": ["string", "null"],
            "description": "ISO date (YYYY-MM-DD) the customer committed to, or null.",
        },
        "customer_intent": {
            "type": ["string", "null"],
            "enum": ["opt_out", "dispute", "broken_instrument", "will_pay", None],
        },
    },
    "required": ["root_cause", "confidence", "reasoning", "promise_to_pay_date", "customer_intent"],
    "additionalProperties": False,
}


@dataclass(frozen=True, slots=True)
class ReasonerResult:
    root_cause: RootCause
    confidence: float
    reasoning: str
    source: str
    promise_to_pay_date: datetime | None = None
    customer_intent: str | None = None


class Reasoner(Protocol):
    name: str

    def diagnose(self, evidence: dict[str, Any]) -> ReasonerResult: ...


# ---------------------------------------------------------------------------
# Real path
# ---------------------------------------------------------------------------


class AnthropicReasoner:
    """Claude, constrained to a JSON schema so the output is always parseable.

    Two deliberate choices worth noting on review:

    * ``output_config.format`` with a json_schema rather than "please reply in
      JSON". The schema is enforced server-side, so there is no parse-retry loop
      and no regex salvage of a half-formatted reply.
    * ``cache_control`` on the system prompt. It is identical across every case
      in the batch, so after the first call it is served from cache. Over a
      400-case run that is the bulk of the input tokens.
    """

    name = "llm"

    def __init__(
        self,
        model: str | None = None,
        effort: str | None = None,
        now: datetime | None = None,
    ) -> None:
        try:
            import anthropic  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover - depends on env
            raise RuntimeError(
                "The anthropic SDK is not installed. Either `pip install anthropic` "
                "or run with RRE_LLM_PROVIDER=offline."
            ) from exc

        self._client = anthropic.Anthropic()
        self.model = model or os.environ.get("RRE_LLM_MODEL", DEFAULT_MODEL)
        self.effort = effort or os.environ.get("RRE_LLM_EFFORT", DEFAULT_EFFORT)
        self._now = now or datetime.now(UTC)
        #: Token accounting, surfaced in the report so the cost of the AI is
        #: visible next to the money it recovered.
        self.input_tokens = 0
        self.output_tokens = 0
        self.cache_read_tokens = 0
        self.calls = 0

    def diagnose(self, evidence: dict[str, Any]) -> ReasonerResult:
        response = self._client.messages.create(
            model=self.model,
            max_tokens=2000,
            system=[
                {
                    "type": "text",
                    "text": SYSTEM_PROMPT,
                    # Stable across the whole batch -> cached after call one.
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"Today is {self._now.date().isoformat()}.\n\n"
                        f"Evidence:\n{json.dumps(evidence, indent=2)}"
                    ),
                }
            ],
            thinking={"type": "adaptive"},
            output_config={
                "effort": self.effort,
                "format": {"type": "json_schema", "schema": DIAGNOSIS_SCHEMA},
            },
        )

        usage = response.usage
        self.calls += 1
        self.input_tokens += getattr(usage, "input_tokens", 0) or 0
        self.output_tokens += getattr(usage, "output_tokens", 0) or 0
        self.cache_read_tokens += getattr(usage, "cache_read_input_tokens", 0) or 0

        text = next(b.text for b in response.content if b.type == "text")
        data = json.loads(text)

        return ReasonerResult(
            root_cause=RootCause(data["root_cause"]),
            confidence=float(data["confidence"]),
            reasoning=data["reasoning"],
            source="llm",
            promise_to_pay_date=_parse_iso_date(data.get("promise_to_pay_date")),
            customer_intent=data.get("customer_intent"),
        )


def _parse_iso_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).replace(tzinfo=UTC)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Control arm
# ---------------------------------------------------------------------------

#: Opt-out and dispute phrasing that plain matching does catch. Kept short on
#: purpose -- this is the honest ceiling of the non-LLM path, and the gap
#: between it and the LLM on the Hinglish replies is the ablation's whole point.
_OPTOUT_PAT = re.compile(
    r"\b(stop|unsubscribe|remove me|do not (message|contact)|don'?t message)\b", re.I
)
_DISPUTE_PAT = re.compile(
    r"\b(charged twice|already paid|paisa kat|double charge|raising it with my bank)\b",
    re.I,
)
_DATE_PAT = re.compile(r"\b(\d{1,2})\s*(?:tarikh|st|nd|rd|th)\b", re.I)


class OfflineReasoner:
    """Deterministic rules over the same evidence. No model, no key, no network.

    This is a real attempt at the problem, not a strawman -- it uses the same
    telemetry the prompt tells Claude to use, including the issuer-vs-rail
    disambiguation. It is beatable mainly where the evidence is genuinely
    ambiguous or buried in free-form Hinglish prose, which is precisely the
    territory the LLM is here for.
    """

    name = "deterministic-fallback"

    def __init__(self, now: datetime | None = None) -> None:
        self._now = now or datetime.now(UTC)
        self.calls = 0

    def diagnose(self, evidence: dict[str, Any]) -> ReasonerResult:
        self.calls += 1
        code = evidence["failure_code"]
        issuer_rate = float(evidence.get("issuer_failure_rate_1h", 0.0))
        rail_rate = float(evidence.get("rail_failure_rate_1h", 0.0))
        attempts = int(evidence.get("attempt_number", 1))
        reply = evidence.get("customer_reply") or ""

        intent: str | None = None
        if _OPTOUT_PAT.search(reply):
            intent = "opt_out"
        elif _DISPUTE_PAT.search(reply):
            intent = "dispute"

        promise = None
        m = _DATE_PAT.search(reply)
        if m:
            day = int(m.group(1))
            if 1 <= day <= 31:
                base = self._now.replace(hour=9, minute=0, second=0, microsecond=0)
                try:
                    cand = base.replace(day=day)
                except ValueError:
                    cand = None
                if cand is not None:
                    promise = cand if cand > base else _add_month(cand)

        cause, confidence, why = self._classify(code, issuer_rate, rail_rate, attempts)

        return ReasonerResult(
            root_cause=cause,
            confidence=confidence,
            reasoning=why,
            source="deterministic-fallback",
            promise_to_pay_date=promise,
            customer_intent=intent,
        )

    @staticmethod
    def _classify(
        code: str, issuer_rate: float, rail_rate: float, attempts: int
    ) -> tuple[RootCause, float, str]:
        # Infrastructure first: a live outage explains almost any code.
        if issuer_rate > 0.30:
            return (
                RootCause.TRANSIENT_ISSUER_OUTAGE,
                0.82,
                f"issuer failure rate {issuer_rate:.0%} indicates a live bank outage",
            )
        if rail_rate > 0.30:
            return (
                RootCause.RAIL_DEGRADATION,
                0.80,
                f"rail failure rate {rail_rate:.0%} with a healthy issuer",
            )

        match code:
            case "card_expired" | "payment_mandate_revoked" | "mandate_expired":
                return RootCause.STALE_INSTRUMENT, 0.90, f"{code} is structural"
            case "authentication_failed" | "otp_timeout":
                return RootCause.AUTH_FRICTION, 0.85, "customer did not complete auth"
            case "incorrect_cvv":
                # Genuinely ambiguous. Repeats lean dead card.
                if attempts >= 2:
                    return RootCause.STALE_INSTRUMENT, 0.58, "repeated cvv failures"
                return RootCause.AUTH_FRICTION, 0.52, "single cvv failure, ambiguous"
            case "risk_declined":
                return RootCause.HARD_DECLINE, 0.88, "explicit risk decline"
            case "do_not_honour":
                # The hard one. No further evidence available to this path.
                return (
                    RootCause.HARD_DECLINE,
                    0.50,
                    "do_not_honour is ambiguous between risk and no-funds",
                )
            case "insufficient_funds":
                return RootCause.INSUFFICIENT_FUNDS, 0.86, "explicit no-funds code"
            case "limit_exceeded":
                return RootCause.INSUFFICIENT_FUNDS, 0.55, "limit hit, cause unclear"
            case "issuer_down":
                return RootCause.TRANSIENT_ISSUER_OUTAGE, 0.80, "explicit issuer down"
            case "gateway_timeout" | "network_error":
                return (
                    RootCause.RAIL_DEGRADATION,
                    0.45,
                    "timeout with no telemetry signal, cause unclear",
                )
            case "checkout_abandoned":
                return RootCause.CUSTOMER_INTENT_LOSS, 0.92, "explicit abandonment"
            case "invoice_overdue":
                return RootCause.RECEIVABLE_OVERDUE, 0.95, "explicit overdue invoice"
            case _:
                return RootCause.HARD_DECLINE, 0.30, "unrecognised code"


def _add_month(d: datetime) -> datetime:
    year, month = (d.year, d.month + 1) if d.month < 12 else (d.year + 1, 1)
    return d.replace(year=year, month=month)


def build_reasoner(
    provider: str | None = None, now: datetime | None = None
) -> Reasoner:
    """Pick a reasoner. Falls back loudly, never silently.

    An explicit ``RRE_LLM_PROVIDER=anthropic`` that cannot be satisfied raises
    rather than quietly degrading -- a run whose provenance is unclear is worse
    than a run that failed.
    """
    provider = provider or os.environ.get("RRE_LLM_PROVIDER", "auto")

    if provider == "offline":
        return OfflineReasoner(now=now)

    if provider == "anthropic":
        return AnthropicReasoner(now=now)

    # auto: use the LLM when a key is present, otherwise the control arm.
    if os.environ.get("ANTHROPIC_API_KEY"):
        try:
            return AnthropicReasoner(now=now)
        except RuntimeError:
            pass
    return OfflineReasoner(now=now)
