"""Synthetic batch generation.

The whole benchmark is worthless if ``failure_code`` maps one-to-one onto
``RootCause`` -- then "diagnosis" is a dict lookup and there is nothing to
demonstrate. So the generator is built around deliberate ambiguity that mirrors
how payments data actually behaves:

* ``do_not_honour`` is what Indian issuers return for *both* a genuine risk
  decline *and* a plain empty account. Same code, opposite correct action.
* ``gateway_timeout`` and ``network_error`` mean "an outage" -- but whether the
  outage is the *issuer* or the *rail* decides whether you wait or reroute. The
  only way to tell is the fleet-wide failure rates carried on the signal.
* ``incorrect_cvv`` is sometimes a dead card and sometimes a fumbled checkout.
* ``limit_exceeded`` splits between a hard bank ceiling and a temporary one.

Roughly a third of cases are unrecoverable no matter what anyone does. A system
that reports recovering all of them is not succeeding, it is miscounting.

Everything is seeded. ``generate(seed=7)`` gives byte-identical output on any
machine, which is what makes the reported numbers checkable by a reviewer.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

from .domain import (
    Case,
    Channel,
    Customer,
    FailureCode,
    Rail,
    RootCause,
    Signal,
)

BANKS = ["HDFC", "ICICI", "SBI", "AXIS", "KOTAK", "PNB", "BOB", "YES"]

FIRST_NAMES = [
    "Aarav", "Diya", "Rohan", "Ananya", "Vikram", "Meera", "Arjun", "Kavya",
    "Siddharth", "Priya", "Rahul", "Neha", "Karthik", "Ishita", "Aditya",
    "Sneha", "Manish", "Pooja", "Nikhil", "Divya", "Farhan", "Zoya",
    "Tanvi", "Harsh", "Ritu", "Sameer", "Lakshmi", "Imran", "Gauri", "Varun",
]
LAST_NAMES = [
    "Sharma", "Patel", "Reddy", "Nair", "Iyer", "Singh", "Gupta", "Menon",
    "Desai", "Chopra", "Bose", "Rao", "Joshi", "Khan", "Verma", "Pillai",
]

#: Free-text replies customers actually send. Several are Hinglish, several
#: carry a payment promise in prose that no regex will reliably catch, and two
#: are unambiguous opt-outs that the agent must honour rather than parse past.
CUSTOMER_REPLIES: list[tuple[str, str]] = [
    ("bhai salary 5 tarikh ko aa rahi hai, uske baad kar dunga payment", "promise"),
    ("Sorry, was travelling. Will clear this by Friday for sure.", "promise"),
    ("card change ho gaya hai, naya wala update karna padega", "instrument"),
    ("STOP. Do not message me again.", "optout"),
    ("Please remove me from your list, I have already cancelled.", "optout"),
    ("paisa kat gaya but order show nahi ho raha, kya scene hai?", "dispute"),
    ("I was charged twice for this. Raising it with my bank.", "dispute"),
    ("Bank ka server down tha kal, aaj try karta hoon", "transient"),
    ("account me balance nahi tha, 2 din me daal dunga", "promise"),
    ("Invoice is with our finance team, payment run happens on the 10th.", "promise"),
    ("net banking kaam nahi kar raha, UPI se kar sakta hoon?", "rail"),
    ("Why do you keep messaging me? I paid this last week.", "dispute"),
    ("thoda time chahiye, month end tak clear kar dunga", "promise"),
    ("My card expired last month, send me a link to update it.", "instrument"),
    ("OTP hi nahi aaya teen baar, phir chhod diya", "auth"),
]


def _pick_case_shape(rng: random.Random) -> tuple[RootCause, FailureCode, bool]:
    """Draw a (true cause, observed symptom, recoverable) triple.

    The symptom is sampled *conditional on* the cause, and several causes share
    symptoms. That conditional overlap is the difficulty of the task.
    """
    roll = rng.random()

    if roll < 0.16:
        cause = RootCause.TRANSIENT_ISSUER_OUTAGE
        code = rng.choice(
            [
                FailureCode.ISSUER_DOWN,
                FailureCode.GATEWAY_TIMEOUT,
                FailureCode.NETWORK_ERROR,
                # An issuer mid-outage sometimes lies and says "no funds".
                FailureCode.INSUFFICIENT_FUNDS,
            ]
        )
        recoverable = rng.random() < 0.88
    elif roll < 0.26:
        cause = RootCause.RAIL_DEGRADATION
        code = rng.choice(
            [
                FailureCode.GATEWAY_TIMEOUT,
                FailureCode.NETWORK_ERROR,
            ]
        )
        recoverable = rng.random() < 0.85
    elif roll < 0.44:
        cause = RootCause.INSUFFICIENT_FUNDS
        code = rng.choice(
            [
                FailureCode.INSUFFICIENT_FUNDS,
                FailureCode.INSUFFICIENT_FUNDS,
                # Issuers routinely return DNH for an empty account.
                FailureCode.DO_NOT_HONOUR,
                FailureCode.LIMIT_EXCEEDED,
            ]
        )
        recoverable = rng.random() < 0.72
    elif roll < 0.58:
        cause = RootCause.STALE_INSTRUMENT
        code = rng.choice(
            [
                FailureCode.CARD_EXPIRED,
                FailureCode.MANDATE_REVOKED,
                FailureCode.MANDATE_EXPIRED,
                FailureCode.INCORRECT_CVV,
            ]
        )
        recoverable = rng.random() < 0.66
    elif roll < 0.70:
        cause = RootCause.AUTH_FRICTION
        code = rng.choice(
            [
                FailureCode.AUTHENTICATION_FAILED,
                FailureCode.OTP_TIMEOUT,
                FailureCode.INCORRECT_CVV,
            ]
        )
        recoverable = rng.random() < 0.80
    elif roll < 0.80:
        cause = RootCause.HARD_DECLINE
        code = rng.choice(
            [
                FailureCode.RISK_DECLINED,
                FailureCode.DO_NOT_HONOUR,
                FailureCode.LIMIT_EXCEEDED,
            ]
        )
        # Most hard declines are exactly as final as they sound.
        recoverable = rng.random() < 0.22
    elif roll < 0.90:
        cause = RootCause.CUSTOMER_INTENT_LOSS
        code = FailureCode.ABANDONED
        recoverable = rng.random() < 0.55
    else:
        cause = RootCause.RECEIVABLE_OVERDUE
        code = FailureCode.INVOICE_OVERDUE
        recoverable = rng.random() < 0.78

    return cause, code, recoverable


def _amount_for(cause: RootCause, rng: random.Random) -> int:
    """Amounts in paise, log-ish spread, B2B invoices an order larger."""
    if cause is RootCause.RECEIVABLE_OVERDUE:
        return rng.randint(50_000_00, 400_000_00)
    if rng.random() < 0.08:
        return rng.randint(1_00, 49_00)  # sub-Rs 50 dust, below the pursue floor
    return rng.randint(199_00, 24_999_00)


def generate(
    n: int = 400, seed: int = 7, now: datetime | None = None
) -> list[Case]:
    """Build a reproducible batch of at-risk payments.

    Returns cases carrying ground truth. Callers that represent the *agent* must
    only ever touch ``case.signal`` and ``case.customer``.
    """
    rng = random.Random(seed)
    now = now or datetime(2026, 9, 5, 14, 30, tzinfo=UTC)
    cases: list[Case] = []

    # A live issuer outage during this batch: one bank is having a bad hour.
    # Cases on this bank carry a high issuer failure rate, which is the evidence
    # a good diagnosis needs and a naive one ignores.
    outage_bank = rng.choice(BANKS)
    degraded_rail = rng.choice([Rail.NETBANKING, Rail.CARD])

    for i in range(n):
        cause, code, recoverable = _pick_case_shape(rng)
        case_id = f"case_{i:04d}"
        cust_id = f"cust_{rng.randint(1000, 1000 + int(n * 0.75)):04d}"

        if cause is RootCause.RECEIVABLE_OVERDUE:
            rail = Rail.NETBANKING
        elif cause is RootCause.STALE_INSTRUMENT:
            rail = rng.choice([Rail.CARD, Rail.EMANDATE, Rail.UPI_AUTOPAY])
        else:
            rail = rng.choice(list(Rail))

        # Fleet telemetry. High issuer rate means the bank is down; high rail
        # rate means the rail is. Both near zero means the problem is this one
        # customer, not the infrastructure.
        issuer_bank = (
            outage_bank
            if cause is RootCause.TRANSIENT_ISSUER_OUTAGE and rng.random() < 0.8
            else rng.choice(BANKS)
        )
        if cause is RootCause.TRANSIENT_ISSUER_OUTAGE and issuer_bank == outage_bank:
            issuer_rate = rng.uniform(0.42, 0.85)
        else:
            issuer_rate = rng.uniform(0.005, 0.06)

        if cause is RootCause.RAIL_DEGRADATION:
            rail = degraded_rail
            rail_rate = rng.uniform(0.38, 0.72)
        else:
            rail_rate = rng.uniform(0.005, 0.05)

        attempt = 1
        priors: tuple[FailureCode, ...] = ()
        if rng.random() < 0.35:
            attempt = rng.randint(2, 4)
            priors = tuple(
                rng.choice([code, FailureCode.GATEWAY_TIMEOUT, FailureCode.NETWORK_ERROR])
                for _ in range(attempt - 1)
            )

        days_overdue = (
            rng.randint(3, 95) if cause is RootCause.RECEIVABLE_OVERDUE else 0
        )

        reply: str | None = None
        reply_kind: str | None = None
        if rng.random() < 0.30:
            reply, reply_kind = rng.choice(CUSTOMER_REPLIES)

        signal = Signal(
            case_id=case_id,
            customer_id=cust_id,
            amount_paise=_amount_for(cause, rng),
            rail=rail,
            failure_code=code,
            occurred_at=now - timedelta(hours=rng.randint(0, 72)),
            attempt_number=attempt,
            prior_failures=priors,
            issuer_bank=issuer_bank,
            issuer_failure_rate_1h=issuer_rate,
            rail_failure_rate_1h=rail_rate,
            days_overdue=days_overdue,
            customer_reply=reply,
            subscription=rng.random() < 0.35,
        )

        channels = {Channel.EMAIL}
        if rng.random() < 0.7:
            channels.add(Channel.SMS)
        if rng.random() < 0.5:
            channels.add(Channel.WHATSAPP)
        if rng.random() < 0.15:
            channels.add(Channel.VOICE)

        # Customers the agent must decline to contact. If these are not present
        # in the batch, the policy layer is never actually tested.
        opted_out = reply_kind == "optout" or rng.random() < 0.07
        dispute_open = reply_kind == "dispute" or rng.random() < 0.05
        ptp: datetime | None = None
        if rng.random() < 0.10:
            ptp = now + timedelta(days=rng.randint(2, 12))

        customer = Customer(
            customer_id=cust_id,
            name=f"{rng.choice(FIRST_NAMES)} {rng.choice(LAST_NAMES)}",
            consented_channels=frozenset(channels),
            opted_out=opted_out,
            promise_to_pay_date=ptp,
            dispute_open=dispute_open,
            contacts_this_week=rng.choice([0, 0, 0, 1, 1, 2, 3]),
        )

        cases.append(
            Case(
                signal=signal,
                customer=customer,
                true_root_cause=cause,
                recoverable=recoverable,
            )
        )

    return cases


def batch_summary(cases: list[Case]) -> dict[str, object]:
    """Descriptive stats for the report header. Uses ground truth on purpose --
    this is the harness describing the world, not the agent claiming anything."""
    from collections import Counter

    at_risk = sum(c.signal.amount_paise for c in cases)
    recoverable = sum(c.signal.amount_paise for c in cases if c.recoverable)
    return {
        "n_cases": len(cases),
        "n_customers": len({c.customer.customer_id for c in cases}),
        "total_at_risk_paise": at_risk,
        "theoretical_max_recoverable_paise": recoverable,
        "cause_mix": dict(Counter(str(c.true_root_cause) for c in cases)),
        "n_opted_out": sum(1 for c in cases if c.customer.opted_out),
        "n_disputes": sum(1 for c in cases if c.customer.dispute_open),
        "n_with_promise": sum(
            1 for c in cases if c.customer.promise_to_pay_date is not None
        ),
        "n_with_reply": sum(1 for c in cases if c.signal.customer_reply),
    }
