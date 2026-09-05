"""Counterfactual arms.

A recovery number with nothing to compare it against is not a result. Two
baselines run over the identical batch, resolved by the identical outcome table:

**``naive_retry``** -- what a large share of merchants actually run today: retry
a failed payment three times on a fixed schedule, diagnose nothing, contact
nobody. It is the honest "before" picture.

**``blast_everyone``** -- the aggressive arm, and the more important one. It
diagnoses nothing but pursues everything: retry *and* message every customer,
including the ones who opted out, the ones with open disputes, and the ones
already contacted three times this week.

The second baseline exists to falsify the easy story. If the agent merely beat
``naive_retry``, the obvious objection would be "you recovered more because you
tried harder, so try harder without the AI and skip the complexity".
``blast_everyone`` tries maximally hard. It is expected to recover **more gross
rupees than the agent does** -- and to be worse off once the cost of doing that
to people is counted.

That is the actual claim of this project: not that the agent recovers the most
money, but that it recovers nearly as much *while not being the kind of system
you would be ashamed to have pointed at your own customers*. If the numbers do
not show that, they get reported anyway.
"""

from __future__ import annotations

import random

from .domain import Case, Intervention, Outcome
from .outcomes import (
    COST_PER_CONTACT_PAISE,
    COST_PER_RETRY_PAISE,
    resolve,
)

MAX_NAIVE_RETRIES = 3


def naive_retry(cases: list[Case], *, seed: int = 11) -> dict[str, Outcome]:
    """Fixed retry schedule, no diagnosis, no contact.

    The retries are independent draws against the same true cause, which is what
    makes this arm lose on structural failures: three attempts at an expired
    card is three times the fee for three times nothing.
    """
    rng = random.Random(seed)
    out: dict[str, Outcome] = {}

    for case in cases:
        recovered = False
        recovered_paise = 0
        notes = "exhausted retries"

        for attempt in range(MAX_NAIVE_RETRIES):
            res = resolve(
                root_cause=case.true_root_cause,
                intervention=Intervention.RETRY_NOW,
                amount_paise=case.signal.amount_paise,
                recoverable=case.recoverable,
                rng=rng,
                contact_index=0,
                is_violation=False,
            )
            if res.recovered:
                recovered = True
                recovered_paise = res.recovered_paise
                notes = f"recovered on retry {attempt + 1}"
                break

        out[case.signal.case_id] = Outcome(
            case_id=case.signal.case_id,
            recovered=recovered,
            recovered_paise=recovered_paise,
            contacted=False,
            contacts_used=0,
            violation=False,
            notes=notes,
        )
    return out


def blast_everyone(cases: list[Case], *, seed: int = 11) -> dict[str, Outcome]:
    """Retry everything and message everyone. No diagnosis, no consent check.

    Violations are counted honestly and priced at ``COST_PER_VIOLATION_PAISE``.
    A customer who opted out, has an open dispute, or is already over the weekly
    cap and gets messaged anyway is one violation each.
    """
    rng = random.Random(seed)
    out: dict[str, Outcome] = {}
    contacts_seen: dict[str, int] = {}

    for case in cases:
        cust = case.customer
        prior = contacts_seen.get(cust.customer_id, 0) + cust.contacts_this_week
        contacts_seen[cust.customer_id] = contacts_seen.get(cust.customer_id, 0) + 1

        kinds: list[str] = []
        if cust.opted_out:
            kinds.append("contacted_after_opt_out")
        if cust.dispute_open:
            kinds.append("chased_during_open_dispute")
        if prior >= 3:
            kinds.append("exceeded_weekly_contact_cap")
        if cust.promise_to_pay_date is not None:
            kinds.append("contacted_before_promised_date")
        if not cust.consented_channels:
            kinds.append("contacted_without_channel_consent")

        recovered = False
        recovered_paise = 0

        # Retry hard first.
        for _ in range(MAX_NAIVE_RETRIES):
            res = resolve(
                root_cause=case.true_root_cause,
                intervention=Intervention.RETRY_NOW,
                amount_paise=case.signal.amount_paise,
                recoverable=case.recoverable,
                rng=rng,
                is_violation=False,
            )
            if res.recovered:
                recovered = True
                recovered_paise = res.recovered_paise
                break

        # Then message regardless of whether anyone wanted to hear from us.
        if not recovered:
            res = resolve(
                root_cause=case.true_root_cause,
                intervention=Intervention.NUDGE_CUSTOMER,
                amount_paise=case.signal.amount_paise,
                recoverable=case.recoverable,
                rng=rng,
                contact_index=min(prior, 3),
                is_violation=bool(kinds),
            )
            if res.recovered:
                recovered = True
                recovered_paise = res.recovered_paise

        out[case.signal.case_id] = Outcome(
            case_id=case.signal.case_id,
            recovered=recovered,
            recovered_paise=recovered_paise,
            contacted=True,
            contacts_used=1,
            violation=bool(kinds),
            violation_kinds=tuple(kinds),
            notes="pursued without gating",
        )
    return out


def naive_cost(cases: list[Case], outcomes: dict[str, Outcome]) -> int:
    """Spend for the naive arm: retries until success, capped."""
    total = 0
    for case in cases:
        o = outcomes[case.signal.case_id]
        attempts = MAX_NAIVE_RETRIES
        if o.recovered and o.notes.startswith("recovered on retry"):
            attempts = int(o.notes.rsplit(" ", 1)[-1])
        total += attempts * COST_PER_RETRY_PAISE
    return total


def blast_cost(cases: list[Case], outcomes: dict[str, Outcome]) -> int:
    """Spend for the aggressive arm, violation cost included."""
    from .outcomes import COST_PER_VIOLATION_PAISE

    total = 0
    for case in cases:
        o = outcomes[case.signal.case_id]
        total += MAX_NAIVE_RETRIES * COST_PER_RETRY_PAISE
        total += COST_PER_CONTACT_PAISE
        if o.violation:
            total += COST_PER_VIOLATION_PAISE
    return total
