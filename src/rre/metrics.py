"""Scoring.

Three things get measured, and the second and third are the ones that keep the
first honest:

1. **Money.** Gross recovered, cost to recover it, and net. Gross alone is a
   vanity metric -- you can always raise it by spending more goodwill, which is
   precisely what the aggressive baseline does.

2. **Diagnosis quality.** Per-cause precision and recall against ground truth,
   plus the confusion pairs. A single accuracy number hides the failure that
   matters: confusing ``hard_decline`` with ``insufficient_funds`` sends a real
   person a message about money they were never going to be charged.

3. **Restraint.** Policy blocks, contacts avoided, and violations. A system that
   recovers slightly less money while never messaging someone who opted out is
   not losing to one that recovers slightly more, and the cost model says so in
   rupees rather than in adjectives.

The exception list is deliberately a first-class output. Every case the agent
declined to resolve automatically is enumerated with its reason. "It handled
everything" is never true, and a report that implies it is, is hiding something.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field

from .domain import (
    CONTACT_INTERVENTIONS,
    Case,
    Intervention,
    Outcome,
    RootCause,
    fmt_inr,
)
from .outcomes import (
    COST_PER_CONTACT_PAISE,
    COST_PER_ESCALATION_PAISE,
    COST_PER_FUTILE_RETRY_PAISE,
    COST_PER_RETRY_PAISE,
)


@dataclass
class ArmMetrics:
    label: str
    n_cases: int
    gross_recovered_paise: int
    cost_paise: int
    n_recovered: int
    n_recoverable: int
    contacts_made: int
    violations: int
    violation_kinds: dict[str, int] = field(default_factory=dict)

    @property
    def net_paise(self) -> int:
        return self.gross_recovered_paise - self.cost_paise

    @property
    def recovery_rate(self) -> float:
        """Share of the *recoverable* money actually recovered.

        Denominated on what was possible, not on everything at risk. Using total
        at-risk as the denominator would flatter every arm equally and hide the
        ceiling.
        """
        return self.n_recovered / self.n_recoverable if self.n_recoverable else 0.0

    @property
    def cost_per_rupee_recovered(self) -> float:
        if not self.gross_recovered_paise:
            return float("inf")
        return self.cost_paise / self.gross_recovered_paise

    def row(self) -> dict[str, str]:
        return {
            "arm": self.label,
            "gross": fmt_inr(self.gross_recovered_paise),
            "cost": fmt_inr(self.cost_paise),
            "net": fmt_inr(self.net_paise),
            "recovered": f"{self.n_recovered}/{self.n_recoverable}",
            "rate": f"{self.recovery_rate:.1%}",
            "contacts": str(self.contacts_made),
            "violations": str(self.violations),
        }


def agent_cost(cases: list[Case]) -> int:
    """Recompute the agent's spend from its own decisions.

    Derived from the decision log rather than accumulated during the run, so the
    reported cost can be checked against the audit trail line by line.
    """
    total = 0
    for case in cases:
        for d in case.decisions:
            if not d.verdict.allowed:
                continue  # a blocked action costs nothing, which is the point
            match d.final:
                case Intervention.RETRY_NOW | Intervention.RETRY_SCHEDULED:
                    total += COST_PER_RETRY_PAISE
                    if d.diagnosis.root_cause in (
                        RootCause.STALE_INSTRUMENT,
                        RootCause.HARD_DECLINE,
                    ):
                        total += COST_PER_FUTILE_RETRY_PAISE
                case Intervention.NUDGE_CUSTOMER | Intervention.REQUEST_NEW_INSTRUMENT:
                    total += COST_PER_CONTACT_PAISE
                case Intervention.ESCALATE_HUMAN:
                    total += COST_PER_ESCALATION_PAISE
                case _:
                    pass
    return total


def score_agent(cases: list[Case]) -> ArmMetrics:
    recoverable = sum(1 for c in cases if c.recoverable)
    gross = sum(c.outcome.recovered_paise for c in cases if c.outcome)
    n_rec = sum(1 for c in cases if c.outcome and c.outcome.recovered)
    contacts = sum(
        1
        for c in cases
        for d in c.decisions
        if d.verdict.allowed and d.final in CONTACT_INTERVENTIONS
    )
    return ArmMetrics(
        label="policy-gated agent",
        n_cases=len(cases),
        gross_recovered_paise=gross,
        cost_paise=agent_cost(cases),
        n_recovered=n_rec,
        n_recoverable=recoverable,
        contacts_made=contacts,
        violations=0,
    )


def score_baseline(
    label: str, cases: list[Case], outcomes: dict[str, Outcome], cost: int
) -> ArmMetrics:
    recoverable = sum(1 for c in cases if c.recoverable)
    gross = sum(o.recovered_paise for o in outcomes.values())
    n_rec = sum(1 for o in outcomes.values() if o.recovered)
    contacts = sum(o.contacts_used for o in outcomes.values())
    kinds: Counter[str] = Counter()
    for o in outcomes.values():
        kinds.update(o.violation_kinds)
    return ArmMetrics(
        label=label,
        n_cases=len(cases),
        gross_recovered_paise=gross,
        cost_paise=cost,
        n_recovered=n_rec,
        n_recoverable=recoverable,
        contacts_made=contacts,
        violations=sum(1 for o in outcomes.values() if o.violation),
        violation_kinds=dict(kinds.most_common()),
    )


# ---------------------------------------------------------------------------
# Diagnosis quality
# ---------------------------------------------------------------------------


@dataclass
class ClassMetric:
    cause: str
    support: int
    tp: int
    fp: int
    fn: int

    @property
    def precision(self) -> float:
        return self.tp / (self.tp + self.fp) if (self.tp + self.fp) else 0.0

    @property
    def recall(self) -> float:
        return self.tp / (self.tp + self.fn) if (self.tp + self.fn) else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if (p + r) else 0.0


def diagnosis_report(cases: list[Case]) -> tuple[list[ClassMetric], list[tuple[str, int]]]:
    """Per-class precision/recall plus the most common confusion pairs."""
    tp: Counter[str] = Counter()
    fp: Counter[str] = Counter()
    fn: Counter[str] = Counter()
    support: Counter[str] = Counter()
    confusions: Counter[tuple[str, str]] = Counter()

    for case in cases:
        if not case.decisions:
            continue
        if case.decisions[0].diagnosis.root_cause is None:
            # No answer is not a wrong answer. Counting outages as predictions
            # once produced a fabricated accuracy figure here; see llm.py.
            continue
        truth = str(case.true_root_cause)
        pred = str(case.decisions[0].diagnosis.root_cause)
        support[truth] += 1
        if pred == truth:
            tp[pred] += 1
        else:
            fp[pred] += 1
            fn[truth] += 1
            confusions[(truth, pred)] += 1

    metrics = [
        ClassMetric(cause=c, support=support[c], tp=tp[c], fp=fp[c], fn=fn[c])
        for c in sorted(support, key=lambda k: -support[k])
    ]
    top_confusions = [
        (f"{t} mistaken for {p}", n) for (t, p), n in confusions.most_common(6)
    ]
    return metrics, top_confusions


def exception_list(cases: list[Case]) -> list[dict[str, str]]:
    """Everything the agent refused to resolve on its own, and why.

    This is the list a merchant's ops team would actually work from tomorrow
    morning. Publishing it is what makes the headline number believable.
    """
    rows: list[dict[str, str]] = []
    for case in cases:
        for d in case.decisions:
            if d.final not in (Intervention.ESCALATE_HUMAN, Intervention.STOP):
                continue
            reason = (
                "; ".join(d.verdict.notes)
                if d.verdict.notes
                else "playbook resolved to no automated action"
            )
            rows.append(
                {
                    "case_id": case.signal.case_id,
                    "amount": fmt_inr(case.signal.amount_paise),
                    "outcome": str(d.final),
                    "believed_cause": str(d.diagnosis.root_cause),
                    "confidence": f"{d.diagnosis.confidence:.2f}",
                    "rules": ", ".join(d.verdict.blocked_by) or "-",
                    "reason": reason,
                }
            )
    rows.sort(key=lambda r: r["case_id"])
    return rows


def contacts_avoided(cases: list[Case]) -> dict[str, int]:
    """Contacts the playbook wanted to send that policy stopped, by rule."""
    counts: dict[str, int] = defaultdict(int)
    for case in cases:
        for d in case.decisions:
            if d.proposed in CONTACT_INTERVENTIONS and not d.verdict.allowed:
                for rule in d.verdict.blocked_by:
                    counts[rule] += 1
    return dict(sorted(counts.items(), key=lambda kv: -kv[1]))
