"""Choosing the fix, once the cause is known.

This is an explicit playbook, not a model call, and that is on purpose.

The hard part of this problem is *diagnosis* -- reading ambiguous evidence and
deciding whether ``do_not_honour`` meant "risk" or "no money". That is where
judgement is needed and where the LLM sits. Once you know the cause, the correct
response is settled domain knowledge: you do not retry an expired card, you wait
out an issuer outage, you route auth friction to a lower-friction rail. Encoding
that as a table makes it reviewable, diffable and testable. Asking a model to
re-derive it on every case would add cost and variance and buy nothing.

Handing the whole decision to a model would also make the system unauditable in
the way that matters: right now a merchant can ask "why did you message this
customer?" and get a straight answer with a rule name attached.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from .domain import Diagnosis, Intervention, Rail, RootCause, Signal

#: Lower-friction alternative for a given rail. UPI drops the 3DS/OTP step that
#: cards impose, which is why it is the standard escape hatch for auth friction.
RAIL_ALTERNATIVE: dict[Rail, Rail] = {
    Rail.CARD: Rail.UPI,
    Rail.NETBANKING: Rail.UPI,
    Rail.WALLET: Rail.UPI,
    Rail.EMANDATE: Rail.UPI_AUTOPAY,
    Rail.UPI_AUTOPAY: Rail.EMANDATE,
    Rail.UPI: Rail.CARD,
}


def choose(
    *, signal: Signal, diagnosis: Diagnosis, now: datetime
) -> tuple[Intervention, datetime | None, str]:
    """Return ``(intervention, scheduled_for, rationale)``.

    ``scheduled_for`` is set when the correct action is "the same thing, later" --
    waiting out an outage, or retrying near payday.
    """
    cause = diagnosis.root_cause

    if cause is None:
        # The reasoner produced nothing. There is no playbook entry for "we do
        # not know", and inventing one is how automated systems act on absence
        # of evidence. A person looks at it.
        return (
            Intervention.ESCALATE_HUMAN,
            None,
            "diagnosis unavailable; no automated action is defensible without one",
        )

    match cause:
        case RootCause.TRANSIENT_ISSUER_OUTAGE:
            # Issuer outages resolve on their own. Retrying into a down bank
            # burns attempts; waiting is both cheaper and more effective.
            when = now + timedelta(hours=4)
            return (
                Intervention.RETRY_SCHEDULED,
                when,
                f"{signal.issuer_bank} is failing at "
                f"{signal.issuer_failure_rate_1h:.0%}; retry after it recovers "
                "rather than burning an attempt now",
            )

        case RootCause.RAIL_DEGRADATION:
            alt = RAIL_ALTERNATIVE.get(signal.rail, Rail.UPI)
            return (
                Intervention.SWITCH_RAIL,
                None,
                f"{signal.rail} is degraded at "
                f"{signal.rail_failure_rate_1h:.0%}; route this attempt via {alt}",
            )

        case RootCause.INSUFFICIENT_FUNDS:
            # Retrying an empty account immediately fails again. Salary lands
            # at month end for most Indian retail customers, so aim there; if
            # that is far off, tell the customer instead of silently waiting.
            days_to_payday = _days_until_payday(now)
            if days_to_payday <= 6:
                return (
                    Intervention.RETRY_SCHEDULED,
                    now + timedelta(days=days_to_payday),
                    f"account is empty; payday is in {days_to_payday}d, retry then "
                    "instead of failing repeatedly against a zero balance",
                )
            return (
                Intervention.NUDGE_CUSTOMER,
                None,
                "account is empty and payday is far off; a single notice lets "
                "the customer act rather than being retried at silently",
            )

        case RootCause.STALE_INSTRUMENT:
            return (
                Intervention.REQUEST_NEW_INSTRUMENT,
                None,
                "the instrument is dead; no number of retries will fix it, the "
                "customer has to supply a new one",
            )

        case RootCause.AUTH_FRICTION:
            alt = RAIL_ALTERNATIVE.get(signal.rail, Rail.UPI)
            return (
                Intervention.SWITCH_RAIL,
                None,
                f"customer dropped out of authentication; {alt} removes the "
                "step they abandoned",
            )

        case RootCause.HARD_DECLINE:
            # The honest answer is usually "stop". Large amounts earn a human,
            # because writing off real money without a person looking is its own
            # failure mode.
            if signal.amount_paise >= 10_000_00:
                return (
                    Intervention.ESCALATE_HUMAN,
                    None,
                    "issuer refused structurally, but the amount is large enough "
                    "to deserve a person rather than an automatic write-off",
                )
            return (
                Intervention.STOP,
                None,
                "issuer refused for a structural reason; further automated "
                "attempts would harass the customer and achieve nothing",
            )

        case RootCause.CUSTOMER_INTENT_LOSS:
            return (
                Intervention.NUDGE_CUSTOMER,
                None,
                "nothing failed technically; the customer left, so one reminder "
                "is the entire available lever",
            )

        case RootCause.RECEIVABLE_OVERDUE:
            # B2B collections escalate with age. Under a month, a reminder does
            # it; past that a human gets better results than any message.
            if signal.days_overdue > 30:
                return (
                    Intervention.ESCALATE_HUMAN,
                    None,
                    f"invoice is {signal.days_overdue}d overdue; past 30d a human "
                    "chaser materially outperforms automated dunning",
                )
            return (
                Intervention.NUDGE_CUSTOMER,
                None,
                f"invoice is {signal.days_overdue}d overdue; a payment reminder "
                "is the proportionate first step",
            )

    return Intervention.STOP, None, "no playbook entry"


def _days_until_payday(now: datetime) -> int:
    """Days until the next month-end, when most Indian salaries land."""
    year, month = (now.year, now.month + 1) if now.month < 12 else (now.year + 1, 1)
    next_month_start = now.replace(
        year=year, month=month, day=1, hour=9, minute=0, second=0, microsecond=0
    )
    return max(0, (next_month_start - now).days)


def compose_message(
    *, signal: Signal, diagnosis: Diagnosis, intervention: Intervention, name: str
) -> str:
    """Templated customer-facing copy.

    Templates rather than generated text, for the same reason the playbook is a
    table: this is the text a real person receives about their money. It should
    be reviewable in advance by a compliance team, identical every time, and
    incapable of hallucinating an amount or a due date.
    """
    from .domain import fmt_inr

    amount = fmt_inr(signal.amount_paise)
    first = name.split()[0]

    if intervention is Intervention.REQUEST_NEW_INSTRUMENT:
        return (
            f"Hi {first}, your payment of {amount} could not go through because "
            "your saved payment method is no longer valid. You can update it "
            "here: [link]. No action needed if you have already done this."
        )
    if intervention is Intervention.NUDGE_CUSTOMER:
        if diagnosis.root_cause is RootCause.RECEIVABLE_OVERDUE:
            return (
                f"Hi {first}, invoice for {amount} is {signal.days_overdue} days "
                "past due. You can settle it here: [link]. If it is already paid "
                "or you would like to discuss it, reply and a person will pick it up."
            )
        if diagnosis.root_cause is RootCause.CUSTOMER_INTENT_LOSS:
            return (
                f"Hi {first}, you left {amount} in your cart. It is still "
                "reserved: [link]. Reply STOP to opt out of these reminders."
            )
        return (
            f"Hi {first}, your payment of {amount} did not go through. You can "
            "complete it here: [link]. Reply STOP to opt out."
        )
    return ""
