"""The world model: what actually happens when you try to recover money.

READ THIS BEFORE BELIEVING ANY NUMBER THIS PROJECT REPORTS.

Every recovery figure in the final report comes out of the probability table
below. The table is the load-bearing assumption of the whole benchmark, so it
gets stated in the open rather than buried:

1. **It was written before the agent was.** Git history shows ``outcomes.py``
   landing ahead of ``diagnose.py``, ``intervene.py`` and ``policy.py``. The
   agent was then built against a fixed world, not tuned against a moving one.

2. **The agent cannot read it.** Nothing under ``rre`` imports this module
   except the harness (``execute.py``) and the baselines. ``tests/test_no_leak.py``
   asserts that. The agent never sees these probabilities, so it cannot exploit
   their exact shape -- it has to actually infer the cause and pick the fix.

3. **The same table scores every arm.** Agent and both baselines are resolved by
   this identical function. Whatever bias the numbers carry, they carry it
   equally for all three, so the *lift* between arms stays meaningful even if
   you disagree with an absolute value.

4. **The numbers are directional, not measured.** They encode well-documented
   payments behaviour -- retrying an expired card is futile, waiting out an
   issuer outage works, UPI carries less auth friction than 3DS cards, dunning
   moves B2B receivables. They are *not* fitted to a real Razorpay dataset,
   because we have none. Treat the shape as the claim and the magnitude as an
   estimate. ``make sensitivity`` re-runs the whole benchmark under +/-30%
   perturbation of this table to show the conclusion survives the assumption.

If you want to attack this project, attack this table. That is the right place
to aim, and it is why it is the first file worth reading.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from .domain import Intervention, RootCause

#: P(money recovered | true root cause, intervention applied), for a case that
#: is recoverable at all. Rows are what is really wrong; columns are what you do
#: about it. The diagonal-ish structure is the entire point: the right fix for
#: one cause is close to useless for another, so blanket strategies lose.
RECOVERY_PROBABILITY: dict[RootCause, dict[Intervention, float]] = {
    RootCause.TRANSIENT_ISSUER_OUTAGE: {
        Intervention.RETRY_NOW: 0.18,  # bank is still down
        Intervention.RETRY_SCHEDULED: 0.72,  # wait it out -- the right fix
        Intervention.SWITCH_RAIL: 0.55,  # route around the outage
        Intervention.NUDGE_CUSTOMER: 0.20,
        Intervention.REQUEST_NEW_INSTRUMENT: 0.12,  # instrument was never the problem
        Intervention.ESCALATE_HUMAN: 0.40,
        Intervention.STOP: 0.05,  # a few customers retry unprompted
    },
    RootCause.RAIL_DEGRADATION: {
        Intervention.RETRY_NOW: 0.15,
        Intervention.RETRY_SCHEDULED: 0.45,
        Intervention.SWITCH_RAIL: 0.78,  # the right fix
        Intervention.NUDGE_CUSTOMER: 0.18,
        Intervention.REQUEST_NEW_INSTRUMENT: 0.15,
        Intervention.ESCALATE_HUMAN: 0.35,
        Intervention.STOP: 0.05,
    },
    RootCause.INSUFFICIENT_FUNDS: {
        Intervention.RETRY_NOW: 0.08,  # the money is still not there
        Intervention.RETRY_SCHEDULED: 0.58,  # retry near payday -- the right fix
        Intervention.SWITCH_RAIL: 0.12,  # no money is no money on any rail
        Intervention.NUDGE_CUSTOMER: 0.42,  # telling them genuinely helps
        Intervention.REQUEST_NEW_INSTRUMENT: 0.25,
        Intervention.ESCALATE_HUMAN: 0.30,
        Intervention.STOP: 0.06,
    },
    RootCause.STALE_INSTRUMENT: {
        Intervention.RETRY_NOW: 0.02,  # futile, and it costs you
        Intervention.RETRY_SCHEDULED: 0.03,  # equally futile
        Intervention.SWITCH_RAIL: 0.30,  # works if another instrument is on file
        Intervention.NUDGE_CUSTOMER: 0.35,
        Intervention.REQUEST_NEW_INSTRUMENT: 0.68,  # the right fix
        Intervention.ESCALATE_HUMAN: 0.40,
        Intervention.STOP: 0.04,
    },
    RootCause.AUTH_FRICTION: {
        Intervention.RETRY_NOW: 0.25,
        Intervention.RETRY_SCHEDULED: 0.30,
        Intervention.SWITCH_RAIL: 0.66,  # UPI drops the 3DS step -- the right fix
        Intervention.NUDGE_CUSTOMER: 0.38,
        Intervention.REQUEST_NEW_INSTRUMENT: 0.22,
        Intervention.ESCALATE_HUMAN: 0.32,
        Intervention.STOP: 0.08,
    },
    RootCause.HARD_DECLINE: {
        Intervention.RETRY_NOW: 0.03,  # the bank meant it
        Intervention.RETRY_SCHEDULED: 0.04,
        Intervention.SWITCH_RAIL: 0.15,
        Intervention.NUDGE_CUSTOMER: 0.10,
        Intervention.REQUEST_NEW_INSTRUMENT: 0.20,
        Intervention.ESCALATE_HUMAN: 0.28,
        Intervention.STOP: 0.02,
    },
    RootCause.CUSTOMER_INTENT_LOSS: {
        Intervention.RETRY_NOW: 0.05,  # there is no charge to retry
        Intervention.RETRY_SCHEDULED: 0.06,
        Intervention.SWITCH_RAIL: 0.10,
        Intervention.NUDGE_CUSTOMER: 0.34,  # a reminder is the whole fix
        Intervention.REQUEST_NEW_INSTRUMENT: 0.12,
        Intervention.ESCALATE_HUMAN: 0.18,
        Intervention.STOP: 0.07,
    },
    RootCause.RECEIVABLE_OVERDUE: {
        Intervention.RETRY_NOW: 0.04,
        Intervention.RETRY_SCHEDULED: 0.10,
        Intervention.SWITCH_RAIL: 0.06,
        Intervention.NUDGE_CUSTOMER: 0.46,  # dunning works on B2B
        Intervention.REQUEST_NEW_INSTRUMENT: 0.10,
        Intervention.ESCALATE_HUMAN: 0.52,  # a human chaser works better still
        Intervention.STOP: 0.05,
    },
}


# --------------------------------------------------------------------------
# Costs. "Money recovered" alone is a vanity metric: you can always recover
# more by spending more goodwill. These are what the spending is worth.
# --------------------------------------------------------------------------

#: Gateway fee burnt on a failed retry attempt, in paise. Small per attempt,
#: real at volume.
COST_PER_RETRY_PAISE = 250  # Rs 2.50

#: Direct send cost of one customer contact, in paise.
COST_PER_CONTACT_PAISE = 40  # Rs 0.40

#: Loaded cost of one human escalation, in paise. An ops person's time.
COST_PER_ESCALATION_PAISE = 12_000  # Rs 120

#: Goodwill cost of one *unwanted* contact, in paise. This is the number most
#: recovery tooling pretends is zero. It is not: it buys unsubscribes, support
#: tickets and churn. Priced deliberately high to make harassment expensive in
#: the same units as the money being chased.
COST_PER_VIOLATION_PAISE = 50_000  # Rs 500

#: Retrying a hard decline is not merely useless -- issuers penalise merchants
#: whose retry-on-decline rate climbs. Charged on top of the retry fee.
COST_PER_FUTILE_RETRY_PAISE = 1_500  # Rs 15

#: Each contact after the first on the same case is less effective than the
#: last. Multiplicative, applied per extra contact.
CONTACT_FATIGUE_DECAY = 0.55


@dataclass(frozen=True, slots=True)
class Resolution:
    """What the world did in response to one intervention."""

    recovered: bool
    recovered_paise: int
    cost_paise: int
    detail: str


def recovery_probability(
    root_cause: RootCause,
    intervention: Intervention,
    *,
    contact_index: int = 0,
    perturbation: dict[RootCause, dict[Intervention, float]] | None = None,
) -> float:
    """Look up P(recovery), with fatigue applied for repeat contacts.

    ``perturbation`` lets the sensitivity harness swap in a shifted table
    without touching the original.
    """
    table = perturbation or RECOVERY_PROBABILITY
    p = table[root_cause][intervention]
    if contact_index > 0:
        p *= CONTACT_FATIGUE_DECAY**contact_index
    return max(0.0, min(1.0, p))


def resolve(
    *,
    root_cause: RootCause,
    intervention: Intervention,
    amount_paise: int,
    recoverable: bool,
    rng: random.Random,
    contact_index: int = 0,
    is_violation: bool = False,
    perturbation: dict[RootCause, dict[Intervention, float]] | None = None,
) -> Resolution:
    """Resolve one intervention against the world.

    Called identically by the agent harness and by both baselines. This symmetry
    is what makes the reported lift a fair comparison rather than a stacked one.
    """
    cost = 0

    if intervention in (Intervention.RETRY_NOW, Intervention.RETRY_SCHEDULED):
        cost += COST_PER_RETRY_PAISE
        if root_cause in (RootCause.STALE_INSTRUMENT, RootCause.HARD_DECLINE):
            # Retrying something the issuer already refused for a structural
            # reason. Costs the fee and a slice of the merchant's standing.
            cost += COST_PER_FUTILE_RETRY_PAISE
    elif intervention in (
        Intervention.NUDGE_CUSTOMER,
        Intervention.REQUEST_NEW_INSTRUMENT,
    ):
        cost += COST_PER_CONTACT_PAISE
    elif intervention is Intervention.ESCALATE_HUMAN:
        cost += COST_PER_ESCALATION_PAISE

    if is_violation:
        cost += COST_PER_VIOLATION_PAISE

    if not recoverable:
        # The money was never coming back. Any effort spent is pure cost --
        # this is where undisciplined systems quietly bleed.
        return Resolution(False, 0, cost, "unrecoverable case, effort wasted")

    p = recovery_probability(
        root_cause, intervention, contact_index=contact_index, perturbation=perturbation
    )
    if rng.random() < p:
        return Resolution(True, amount_paise, cost, f"recovered at p={p:.2f}")
    return Resolution(False, 0, cost, f"no recovery at p={p:.2f}")


def perturbed_table(
    rng: random.Random, magnitude: float = 0.30
) -> dict[RootCause, dict[Intervention, float]]:
    """A copy of the table with every cell independently jittered.

    Used by ``make sensitivity``. If the agent's advantage over the baselines
    only exists at the exact numbers hand-written above, that advantage is an
    artefact and deserves to be caught here.
    """
    out: dict[RootCause, dict[Intervention, float]] = {}
    for cause, row in RECOVERY_PROBABILITY.items():
        out[cause] = {
            iv: max(0.0, min(1.0, p * (1.0 + rng.uniform(-magnitude, magnitude))))
            for iv, p in row.items()
        }
    return out
