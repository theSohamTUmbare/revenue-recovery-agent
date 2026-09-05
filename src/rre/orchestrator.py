"""The agent loop.

Two stages, split on a property that matters:

**Stage 1 -- diagnosis, parallel.** Diagnosing a case depends only on that
case's own evidence. It is stateless, so it fans out across a thread pool. With
the LLM reasoner this is the slow part, and it is where the concurrency pays.

**Stage 2 -- decide and act, strictly sequential.** The policy ledger is
order-dependent by design: whether this customer may be contacted depends on how
many times they have *already* been contacted during this run. Parallelising
stage 2 would introduce a race on exactly the counter that stops us
double-messaging someone, so it runs in a fixed, deterministic order. The whole
point of a contact cap is lost if two workers can both observe "2 of 3 used" and
both proceed.

That asymmetry is the reason the two stages are separated at all.
"""

from __future__ import annotations

import concurrent.futures
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from .audit import AuditLog, rollback_for
from .domain import (
    CONTACT_INTERVENTIONS,
    Case,
    Decision,
    Diagnosis,
    Intervention,
    Outcome,
    PolicyConfig,
)
from .intervene import choose, compose_message
from .llm import Reasoner, build_reasoner
from .outcomes import resolve
from .policy import PolicyEngine


@dataclass
class RunResult:
    label: str
    cases: list[Case]
    audit: AuditLog
    policy_rule_fires: dict[str, int]
    reasoner_name: str
    diagnosis_correct: int = 0
    diagnosis_total: int = 0
    #: Cases the reasoner could not answer at all. Never silently zero.
    undiagnosed: int = 0
    llm_usage: dict[str, int] = field(default_factory=dict)


def _diagnose_one(reasoner: Reasoner, case: Case) -> Diagnosis:
    """Stage 1 worker. Sees ``case.signal`` only -- never ``true_root_cause``."""
    result = reasoner.diagnose(case.signal.to_evidence())
    return Diagnosis(
        root_cause=result.root_cause,
        confidence=result.confidence,
        reasoning=result.reasoning,
        source=result.source,
        extracted_promise_date=result.promise_to_pay_date,
    )


def run_agent(
    cases: list[Case],
    *,
    reasoner: Reasoner | None = None,
    policy_config: PolicyConfig | None = None,
    now: datetime | None = None,
    seed: int = 11,
    audit_path: Path | None = None,
    max_workers: int = 8,
    progress: bool = False,
) -> RunResult:
    """Run the full pipeline over a batch."""
    import random

    now = now or datetime(2026, 9, 5, 22, 15, tzinfo=UTC)
    reasoner = reasoner or build_reasoner(now=now)
    engine = PolicyEngine(policy_config)
    audit = AuditLog(path=audit_path)
    rng = random.Random(seed)

    # -- Stage 1: diagnose, in parallel ---------------------------------
    diagnoses: dict[str, Diagnosis] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(_diagnose_one, reasoner, case): case for case in cases
        }
        done = 0
        for fut in concurrent.futures.as_completed(futures):
            case = futures[fut]
            diagnoses[case.signal.case_id] = fut.result()
            done += 1
            if progress and done % 25 == 0:
                print(f"  diagnosed {done}/{len(cases)}", flush=True)

    # Accuracy is computed over cases the reasoner actually answered. Cases it
    # could not answer are reported separately rather than folded in as errors,
    # because an unreachable provider is an absence of measurement, not a wrong
    # measurement.
    answered = [
        c for c in cases if diagnoses[c.signal.case_id].root_cause is not None
    ]
    undiagnosed = len(cases) - len(answered)
    correct = sum(
        1 for c in answered if diagnoses[c.signal.case_id].root_cause == c.true_root_cause
    )

    # -- Stage 2: decide and act, in a fixed order ----------------------
    for case in cases:
        diagnosis = diagnoses[case.signal.case_id]
        proposed, scheduled, rationale = choose(
            signal=case.signal, diagnosis=diagnosis, now=now
        )
        verdict = engine.evaluate(
            signal=case.signal,
            customer=case.customer,
            diagnosis=diagnosis,
            proposed=proposed,
            now=now,
        )

        final = proposed if verdict.allowed else (verdict.substituted or Intervention.STOP)
        if verdict.scheduled_for is not None:
            scheduled = verdict.scheduled_for

        message = None
        if verdict.allowed and final in CONTACT_INTERVENTIONS:
            message = compose_message(
                signal=case.signal,
                diagnosis=diagnosis,
                intervention=final,
                name=case.customer.name,
            )

        decision = Decision(
            case_id=case.signal.case_id,
            proposed=proposed,
            final=final,
            diagnosis=diagnosis,
            verdict=verdict,
            decided_at=now,
            scheduled_for=scheduled,
            channel=verdict.channel,
            message=message,
            rollback=rollback_for(final, scheduled),
        )
        case.decisions.append(decision)
        audit.record(decision, extra={"playbook_rationale": rationale})

        # -- resolve against the world ---------------------------------
        contacted = final in CONTACT_INTERVENTIONS and verdict.allowed
        res = resolve(
            root_cause=case.true_root_cause,
            intervention=final,
            amount_paise=case.signal.amount_paise,
            recoverable=case.recoverable,
            rng=rng,
            contact_index=0,
            # The gated agent cannot produce a violation: every path that would
            # touch an opted-out, disputing or over-contacted customer has
            # already been converted to STOP or ESCALATE_HUMAN above. The
            # baselines have no such gate, and their violation counts show it.
            is_violation=False,
        )
        case.outcome = Outcome(
            case_id=case.signal.case_id,
            recovered=res.recovered,
            recovered_paise=res.recovered_paise,
            contacted=contacted,
            contacts_used=1 if contacted else 0,
            violation=False,
            notes=res.detail,
        )

    audit.flush()

    usage: dict[str, int] = {}
    for attr in ("calls", "input_tokens", "output_tokens", "cache_read_tokens"):
        if hasattr(reasoner, attr):
            usage[attr] = getattr(reasoner, attr)

    return RunResult(
        label="policy-gated agent",
        cases=cases,
        audit=audit,
        policy_rule_fires=engine.snapshot(),
        reasoner_name=reasoner.name,
        diagnosis_correct=correct,
        diagnosis_total=len(answered),
        undiagnosed=undiagnosed,
        llm_usage=usage,
    )
