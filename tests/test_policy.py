"""Guardrail tests.

policy.py claims the contact rules 'have a correct answer that does not vary
with temperature, prompt wording, or model version, and should be answered by
something you can unit-test to a fixed point'. This file is that fixed point.

Each test encodes one obligation. If a future refactor makes it possible to
message someone who opted out, one of these goes red.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from rre.domain import (
    Channel,
    Customer,
    Diagnosis,
    FailureCode,
    Intervention,
    PolicyConfig,
    Rail,
    RootCause,
    Signal,
)
from rre.policy import PolicyEngine

NOON = datetime(2026, 9, 7, 6, 30, tzinfo=UTC)  # 12:00 IST -- outside quiet hours
NIGHT = datetime(2026, 9, 7, 18, 30, tzinfo=UTC)  # 00:00 IST -- inside quiet hours


def _customer(**kw) -> Customer:
    base = dict(
        customer_id="cust_0001",
        name="Test Person",
        consented_channels=frozenset({Channel.EMAIL, Channel.SMS}),
        opted_out=False,
        contacts_this_week=0,
    )
    base.update(kw)
    return Customer(**base)


def _signal(**kw) -> Signal:
    base = dict(
        case_id="case_0001",
        customer_id="cust_0001",
        amount_paise=500_00,
        rail=Rail.CARD,
        failure_code=FailureCode.INSUFFICIENT_FUNDS,
        occurred_at=NOON,
        attempt_number=1,
    )
    base.update(kw)
    return Signal(**base)


def _diagnosis(cause=RootCause.INSUFFICIENT_FUNDS, confidence=0.9, **kw) -> Diagnosis:
    return Diagnosis(
        root_cause=cause,
        confidence=confidence,
        reasoning="test",
        source="test",
        **kw,
    )


def _evaluate(engine, *, signal=None, customer=None, diagnosis=None, proposed, now=NOON):
    return engine.evaluate(
        signal=signal or _signal(),
        customer=customer or _customer(),
        diagnosis=diagnosis or _diagnosis(),
        proposed=proposed,
        now=now,
    )


# -- absolute prohibitions -------------------------------------------------


def test_opted_out_customer_is_never_contacted() -> None:
    """No amount, confidence, or business case overrides an opt-out."""
    engine = PolicyEngine()
    verdict = _evaluate(
        engine,
        customer=_customer(opted_out=True),
        signal=_signal(amount_paise=50_000_00),  # a lot of money
        proposed=Intervention.NUDGE_CUSTOMER,
    )
    assert not verdict.allowed
    assert "opted_out" in verdict.blocked_by
    assert verdict.substituted is Intervention.STOP


def test_open_dispute_suspends_all_automated_recovery() -> None:
    engine = PolicyEngine()
    for proposed in (
        Intervention.NUDGE_CUSTOMER,
        Intervention.RETRY_NOW,
        Intervention.SWITCH_RAIL,
    ):
        verdict = _evaluate(
            engine, customer=_customer(dispute_open=True), proposed=proposed
        )
        assert not verdict.allowed, f"{proposed} should be blocked during a dispute"
        assert "dispute_open" in verdict.blocked_by
        assert verdict.substituted is Intervention.ESCALATE_HUMAN


def test_promise_to_pay_is_respected_until_the_date() -> None:
    engine = PolicyEngine()
    promised = NOON + timedelta(days=5)
    verdict = _evaluate(
        engine,
        customer=_customer(promise_to_pay_date=promised),
        proposed=Intervention.NUDGE_CUSTOMER,
    )
    assert not verdict.allowed
    assert "promise_to_pay_hold" in verdict.blocked_by


def test_promise_extracted_from_free_text_is_honoured_too() -> None:
    """A promise the model read out of a Hinglish reply binds exactly as hard.

    The reasoner is allowed to *find* the commitment; the policy layer is what
    enforces it. This checks the handoff works.
    """
    engine = PolicyEngine()
    verdict = _evaluate(
        engine,
        customer=_customer(),  # nothing on the customer record
        diagnosis=_diagnosis(extracted_promise_date=NOON + timedelta(days=3)),
        proposed=Intervention.NUDGE_CUSTOMER,
    )
    assert not verdict.allowed
    assert "promise_to_pay_hold" in verdict.blocked_by


# -- caps ------------------------------------------------------------------


def test_weekly_cap_counts_across_cases_not_per_case() -> None:
    """Four invoices must not become four messages.

    Each contact is individually defensible; the aggregate is harassment. This
    is the bug the cross-case ledger exists to prevent.
    """
    engine = PolicyEngine(PolicyConfig(max_contacts_per_customer_per_week=3))
    cust = _customer()
    allowed = 0
    for i in range(6):
        verdict = engine.evaluate(
            signal=_signal(case_id=f"case_{i:04d}"),
            customer=cust,
            diagnosis=_diagnosis(),
            proposed=Intervention.NUDGE_CUSTOMER,
            now=NOON + timedelta(days=3 * i),  # spaced out, so cooloff never fires
        )
        if verdict.allowed:
            allowed += 1
        else:
            assert "contact_cap_customer" in verdict.blocked_by
    assert allowed == 3, f"weekly cap breached: {allowed} contacts allowed"


def test_prior_contacts_on_the_record_count_toward_the_cap() -> None:
    """Someone already messaged twice this week has one left, not three."""
    engine = PolicyEngine(PolicyConfig(max_contacts_per_customer_per_week=3))
    cust = _customer(contacts_this_week=3)
    verdict = _evaluate(engine, customer=cust, proposed=Intervention.NUDGE_CUSTOMER)
    assert not verdict.allowed
    assert "contact_cap_customer" in verdict.blocked_by


def test_cooloff_blocks_rapid_repeat_contact() -> None:
    engine = PolicyEngine()
    cust = _customer()
    first = _evaluate(engine, customer=cust, proposed=Intervention.NUDGE_CUSTOMER)
    assert first.allowed
    second = engine.evaluate(
        signal=_signal(case_id="case_0002"),
        customer=cust,
        diagnosis=_diagnosis(),
        proposed=Intervention.NUDGE_CUSTOMER,
        now=NOON + timedelta(hours=2),
    )
    assert not second.allowed
    assert "cooloff" in second.blocked_by


# -- substitutions ---------------------------------------------------------


def test_quiet_hours_defer_rather_than_cancel() -> None:
    """The right answer at midnight is 'later', not 'never'.

    A gate that only knows yes and no would throw away the recovery to avoid the
    1am SMS. This checks it reschedules into the allowed window instead.
    """
    engine = PolicyEngine()
    verdict = _evaluate(engine, proposed=Intervention.NUDGE_CUSTOMER, now=NIGHT)
    assert verdict.allowed, "quiet hours should defer, not block"
    assert verdict.scheduled_for is not None
    local_hour = (
        verdict.scheduled_for + timedelta(minutes=330)
    ).hour
    assert 9 <= local_hour < 21, f"rescheduled into quiet hours: {local_hour}:00"


def test_low_confidence_routes_to_a_human_instead_of_guessing() -> None:
    engine = PolicyEngine()
    verdict = _evaluate(
        engine,
        diagnosis=_diagnosis(confidence=0.31),
        proposed=Intervention.NUDGE_CUSTOMER,
    )
    assert not verdict.allowed
    assert "low_confidence" in verdict.blocked_by
    assert verdict.substituted is Intervention.ESCALATE_HUMAN


def test_futile_retries_are_blocked_and_redirected() -> None:
    """Retrying a dead card is stopped, and turned into the fix that works."""
    engine = PolicyEngine()
    verdict = _evaluate(
        engine,
        diagnosis=_diagnosis(cause=RootCause.STALE_INSTRUMENT),
        proposed=Intervention.RETRY_NOW,
    )
    assert not verdict.allowed
    assert "futile_retry" in verdict.blocked_by
    assert verdict.substituted is Intervention.REQUEST_NEW_INSTRUMENT


def test_hard_decline_retry_is_stopped_outright() -> None:
    engine = PolicyEngine()
    verdict = _evaluate(
        engine,
        diagnosis=_diagnosis(cause=RootCause.HARD_DECLINE),
        proposed=Intervention.RETRY_NOW,
    )
    assert not verdict.allowed
    assert verdict.substituted is Intervention.STOP


def test_dust_amounts_are_not_pursued() -> None:
    engine = PolicyEngine()
    verdict = _evaluate(
        engine, signal=_signal(amount_paise=12_00), proposed=Intervention.NUDGE_CUSTOMER
    )
    assert not verdict.allowed
    assert "below_pursue_floor" in verdict.blocked_by


def test_channel_must_be_consented() -> None:
    engine = PolicyEngine()
    verdict = _evaluate(
        engine,
        customer=_customer(consented_channels=frozenset()),
        proposed=Intervention.NUDGE_CUSTOMER,
    )
    assert not verdict.allowed
    assert "no_consented_channel" in verdict.blocked_by


def test_chosen_channel_is_always_one_the_customer_consented_to() -> None:
    engine = PolicyEngine()
    verdict = _evaluate(
        engine,
        customer=_customer(consented_channels=frozenset({Channel.WHATSAPP})),
        proposed=Intervention.NUDGE_CUSTOMER,
    )
    assert verdict.allowed
    assert verdict.channel is Channel.WHATSAPP


# -- the aggregate property ------------------------------------------------


def test_no_violation_is_reachable_on_a_full_batch() -> None:
    """End-to-end: over a real batch, zero prohibited contacts occur.

    The per-rule tests above check each gate. This checks they compose -- that
    there is no path through the whole pipeline that reaches a person who should
    not have been reached.
    """
    from rre.domain import CONTACT_INTERVENTIONS
    from rre.generate import generate
    from rre.llm import OfflineReasoner
    from rre.orchestrator import run_agent

    cases = generate(400, seed=7)
    run_agent(cases, reasoner=OfflineReasoner(), max_workers=4)

    for case in cases:
        for d in case.decisions:
            if d.final in CONTACT_INTERVENTIONS and d.verdict.allowed:
                assert not case.customer.opted_out, (
                    f"{case.signal.case_id}: contacted an opted-out customer"
                )
                assert not case.customer.dispute_open, (
                    f"{case.signal.case_id}: contacted during an open dispute"
                )
