"""Break-even analysis on the price of a violation.

This module exists because the benchmark produced an inconvenient result and we
decided to publish it rather than tune it away.

**The result.** At the cost model's default price of Rs 500 per violation, the
aggressive baseline -- which retries everything and messages everyone, including
customers who opted out, customers with open disputes, and customers already at
their weekly contact cap -- comes out *ahead of the policy-gated agent on net
rupees*. It recovers more money. Harassment, priced at Rs 500 a head, pays.

**What we did not do about it.** The obvious move is to raise
``COST_PER_VIOLATION_PAISE`` until the agent wins. That would be fitting the
world model to the desired conclusion, which is the exact failure this project
claims to guard against. The constant is unchanged.

**What we did instead.** We compute the violation price at which the two arms
break even, and let a reader judge whether the real number is above or below it.
That converts a hidden assumption into a stated, checkable one.

**Why we would still not ship the aggressive arm.** Because "cost per violation"
is the wrong frame for most of what it does, and the model quietly assumes the
harm is a fine you can pay:

* Messaging a customer who has opted out is not a cost centre. Under the DPDP
  Act 2023, processing personal data after consent is withdrawn is a compliance
  breach, and TRAI's UCC regulations treat repeat unsolicited commercial
  communication as grounds for disconnection of the sending resource.
* Chasing a customer during an open dispute prejudices the merchant's own
  position in that dispute.
* The model prices each violation once. In reality one aggrieved customer
  produces a support ticket, a chargeback, a public review, and a churn event
  from a single message.

A cost model that can express "this is expensive" cannot express "this is not
ours to do". The break-even number below is the honest version of the argument;
the paragraph above is the reason the agent is still the design we would ship.
"""

from __future__ import annotations

from dataclasses import dataclass

from .domain import fmt_inr
from .metrics import ArmMetrics
from .outcomes import COST_PER_VIOLATION_PAISE


@dataclass(frozen=True, slots=True)
class BreakEven:
    agent_net_paise: int
    rival_net_paise: int
    rival_label: str
    violations: int
    current_price_paise: int
    breakeven_price_paise: int | None
    agent_wins_at_current_price: bool

    @property
    def gap_paise(self) -> int:
        return self.agent_net_paise - self.rival_net_paise

    def summary_lines(self) -> list[str]:
        lines: list[str] = []
        if self.agent_wins_at_current_price:
            lines.append(
                f"  The agent is ahead of '{self.rival_label}' on net by "
                f"{fmt_inr(self.gap_paise)} at the current "
                f"{fmt_inr(self.current_price_paise)} violation price."
            )
        else:
            lines.append(
                f"  HONEST RESULT: '{self.rival_label}' beats the agent on net by "
                f"{fmt_inr(-self.gap_paise)}."
            )
            lines.append(
                f"  At {fmt_inr(self.current_price_paise)} per violation, pursuing "
                f"{self.violations} people without a gate is profitable."
            )
        if self.breakeven_price_paise is not None:
            lines.append(
                f"  Break-even violation price: {fmt_inr(self.breakeven_price_paise)}. "
                "Above this, restraint pays for itself on rupees alone."
            )
            lines.append(
                "  We did not raise the constant to clear that bar. The reasons the "
                "agent is still the shippable design are regulatory, not arithmetic "
                "-- see src/rre/breakeven.py."
            )
        return lines


def analyse(agent: ArmMetrics, rival: ArmMetrics) -> BreakEven:
    """Find the per-violation price at which ``agent`` matches ``rival`` on net.

    The rival's cost already contains ``violations * current_price``. Strip that
    out to get its violation-free cost, then solve for the price ``p`` where the
    two nets are equal.
    """
    rival_cost_excl = rival.cost_paise - rival.violations * COST_PER_VIOLATION_PAISE
    rival_net_excl = rival.gross_recovered_paise - rival_cost_excl

    breakeven: int | None = None
    if rival.violations > 0:
        # agent_net == rival_net_excl - violations * p
        p = (rival_net_excl - agent.net_paise) / rival.violations
        breakeven = int(round(p)) if p > 0 else 0

    return BreakEven(
        agent_net_paise=agent.net_paise,
        rival_net_paise=rival.net_paise,
        rival_label=rival.label,
        violations=rival.violations,
        current_price_paise=COST_PER_VIOLATION_PAISE,
        breakeven_price_paise=breakeven,
        agent_wins_at_current_price=agent.net_paise >= rival.net_paise,
    )


def per_case_harm(rival: ArmMetrics, n_cases: int) -> list[tuple[str, int, str]]:
    """Violations by kind, with the obligation each one actually breaches.

    The third element is deliberately not a rupee figure. Some of these are not
    priced in rupees at all, which is the point being made.
    """
    mapping = {
        "contacted_after_opt_out": (
            "DPDP Act 2023: processing after consent withdrawal; "
            "TRAI UCC: unsolicited commercial communication"
        ),
        "chased_during_open_dispute": (
            "prejudices the merchant's own position in the dispute; "
            "card-network evidence rules"
        ),
        "exceeded_weekly_contact_cap": (
            "TRAI UCC complaint threshold; the practical definition of harassment"
        ),
        "contacted_before_promised_date": (
            "breaks a commitment the customer was asked to make; "
            "destroys the value of ever asking again"
        ),
        "contacted_without_channel_consent": (
            "DPDP Act 2023: no lawful basis for the channel used"
        ),
    }
    rows: list[tuple[str, int, str]] = []
    for kind, count in rival.violation_kinds.items():
        rows.append((kind, count, mapping.get(kind, "unclassified")))
    return rows
