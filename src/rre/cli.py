"""Command line entry point.

    python -m rre demo            full benchmark, all three arms
    python -m rre ablation        does the LLM actually beat rules?
    python -m rre sensitivity     does the conclusion survive a shaken table?
    python -m rre audit           show sample audit entries
    python -m rre report          write the HTML report
"""

from __future__ import annotations

import argparse
import random
import sys
from datetime import UTC, datetime
from pathlib import Path

from .baselines import blast_cost, blast_everyone, naive_cost, naive_retry
from .breakeven import analyse, per_case_harm
from .domain import fmt_inr
from .generate import batch_summary, generate
from .llm import OfflineReasoner, build_reasoner
from .metrics import (
    ArmMetrics,
    contacts_avoided,
    diagnosis_report,
    exception_list,
    score_agent,
    score_baseline,
)
from .orchestrator import run_agent

REPORTS = Path("reports")
NOW = datetime(2026, 9, 5, 22, 15, tzinfo=UTC)


def _rule(char: str = "-", width: int = 78) -> str:
    return char * width


def _table(rows: list[dict[str, str]], headers: list[str]) -> str:
    if not rows:
        return "  (none)"
    widths = {
        h: max(len(h), max(len(str(r.get(h, ""))) for r in rows)) for h in headers
    }
    out = ["  " + "  ".join(h.upper().ljust(widths[h]) for h in headers)]
    out.append("  " + "  ".join("-" * widths[h] for h in headers))
    for r in rows:
        out.append("  " + "  ".join(str(r.get(h, "")).ljust(widths[h]) for h in headers))
    return "\n".join(out)


def cmd_demo(args: argparse.Namespace) -> int:
    cases = generate(args.n, seed=args.seed, now=NOW)
    summary = batch_summary(cases)

    print()
    print(_rule("="))
    print("  REVENUE RECOVERY AGENT - BATCH RUN")
    print(_rule("="))
    print(f"  cases            {summary['n_cases']}")
    print(f"  customers        {summary['n_customers']}")
    print(f"  total at risk    {fmt_inr(summary['total_at_risk_paise'])}")
    print(
        f"  recoverable      {fmt_inr(summary['theoretical_max_recoverable_paise'])}"
        "   <- nobody can beat this ceiling"
    )
    print(
        f"  sensitive        {summary['n_opted_out']} opted out, "
        f"{summary['n_disputes']} disputes, {summary['n_with_promise']} promises to pay"
    )

    reasoner = build_reasoner(now=NOW)
    print(f"  reasoner         {reasoner.name}")
    if reasoner.name == "deterministic-fallback":
        print("                   (no ANTHROPIC_API_KEY found - running control arm)")
    print()

    REPORTS.mkdir(exist_ok=True)
    result = run_agent(
        cases,
        reasoner=reasoner,
        now=NOW,
        seed=args.seed,
        audit_path=REPORTS / "audit.jsonl",
        progress=args.progress,
    )

    agent = score_agent(cases)
    naive_out = naive_retry(cases, seed=args.seed)
    blast_out = blast_everyone(cases, seed=args.seed)
    naive = score_baseline("naive retry x3", cases, naive_out, naive_cost(cases, naive_out))
    blast = score_baseline(
        "blast everyone", cases, blast_out, blast_cost(cases, blast_out)
    )

    print(_rule("="))
    print("  ARM COMPARISON  (identical batch, identical outcome model)")
    print(_rule("="))
    print(
        _table(
            [agent.row(), naive.row(), blast.row()],
            ["arm", "gross", "cost", "net", "recovered", "rate", "contacts", "violations"],
        )
    )
    print()
    lift = agent.net_paise - naive.net_paise
    print(f"  net lift over naive retry:   {fmt_inr(lift)}")
    print(f"  and it did that while making {agent.contacts_made} contacts, not {blast.contacts_made}.")
    print()

    be = analyse(agent, blast)
    print(_rule())
    for line in be.summary_lines():
        print(line)
    print(_rule())
    print()
    print(f"  What the aggressive arm did to {blast.violations} people to earn that:")
    for kind, n, obligation in per_case_harm(blast, len(cases)):
        print(f"      {n:4d}  {kind}")
        print(f"            {obligation}")

    print()
    print(_rule("="))
    print("  DIAGNOSIS QUALITY  (vs held-out ground truth)")
    print(_rule("="))
    acc = result.diagnosis_correct / max(1, result.diagnosis_total)
    print(f"  overall accuracy   {acc:.1%}  ({result.diagnosis_correct}/{result.diagnosis_total})")
    print()
    metrics, confusions = diagnosis_report(cases)
    print(
        _table(
            [
                {
                    "cause": m.cause,
                    "n": str(m.support),
                    "precision": f"{m.precision:.2f}",
                    "recall": f"{m.recall:.2f}",
                    "f1": f"{m.f1:.2f}",
                }
                for m in metrics
            ],
            ["cause", "n", "precision", "recall", "f1"],
        )
    )
    if confusions:
        print()
        print("  most costly confusions:")
        for label, n in confusions:
            print(f"      {n:4d}  {label}")

    print()
    print(_rule("="))
    print("  RESTRAINT  (what the policy gate stopped)")
    print(_rule("="))
    fires = result.policy_rule_fires
    if fires:
        for rule, n in fires.items():
            print(f"      {n:4d}  {rule}")
    avoided = contacts_avoided(cases)
    total_avoided = sum(avoided.values())
    print()
    print(f"  contacts the playbook wanted, that policy refused:  {total_avoided}")
    print(f"  violations committed by the agent:                  {agent.violations}")

    exceptions = exception_list(cases)
    print()
    print(_rule("="))
    print(f"  EXCEPTION LIST  ({len(exceptions)} cases not resolved automatically)")
    print(_rule("="))
    print(_table(exceptions[:12], ["case_id", "amount", "outcome", "believed_cause", "rules"]))
    if len(exceptions) > 12:
        print(f"  ... and {len(exceptions) - 12} more (full list in the HTML report)")

    if result.llm_usage and result.reasoner_name == "llm":
        print()
        print(_rule("="))
        print("  WHAT THE AI COST")
        print(_rule("="))
        u = result.llm_usage
        print(f"  calls              {u.get('calls', 0)}")
        print(f"  input tokens       {u.get('input_tokens', 0):,}")
        print(f"  cached reads       {u.get('cache_read_tokens', 0):,}")
        print(f"  output tokens      {u.get('output_tokens', 0):,}")

    print()
    print(f"  audit trail written to {REPORTS / 'audit.jsonl'} ({len(result.audit.entries)} entries)")
    print()

    from .report import write_html

    path = write_html(
        cases=cases,
        result=result,
        arms=[agent, naive, blast],
        summary=summary,
        out=REPORTS / "report.html",
    )
    print(f"  HTML report written to {path}")
    print()
    return 0


def cmd_ablation(args: argparse.Namespace) -> int:
    """Does the LLM earn its place? Same batch, both reasoners, diffed."""
    print()
    print(_rule("="))
    print("  ABLATION - is the LLM actually doing work?")
    print(_rule("="))
    print("  Same batch, same policy, same outcome model. Only the reasoner changes.")
    print()

    rows = []
    for name, reasoner in (
        ("deterministic rules", OfflineReasoner(now=NOW)),
        ("claude", build_reasoner("anthropic", now=NOW) if args.llm else None),
    ):
        if reasoner is None:
            print("  (skipping the LLM arm; pass --llm and set ANTHROPIC_API_KEY)")
            continue
        cases = generate(args.n, seed=args.seed, now=NOW)
        result = run_agent(cases, reasoner=reasoner, now=NOW, seed=args.seed)
        agent = score_agent(cases)
        acc = result.diagnosis_correct / max(1, result.diagnosis_total)
        rows.append(
            {
                "reasoner": name,
                "accuracy": f"{acc:.1%}",
                "net": fmt_inr(agent.net_paise),
                "recovered": f"{agent.n_recovered}/{agent.n_recoverable}",
                "contacts": str(agent.contacts_made),
            }
        )

    print(_table(rows, ["reasoner", "accuracy", "net", "recovered", "contacts"]))
    print()
    print("  If these two rows are identical, the LLM is decoration and should be")
    print("  removed. The gap is the honest measure of what it contributes.")
    print()
    return 0


def cmd_sensitivity(args: argparse.Namespace) -> int:
    """Does the conclusion survive if the outcome table is wrong?"""
    from .outcomes import perturbed_table

    print()
    print(_rule("="))
    print("  SENSITIVITY - shaking the outcome table")
    print(_rule("="))
    print(f"  {args.trials} trials, every probability jittered by +/-30%.")
    print("  If the agent only wins at the exact hand-written numbers, that is an")
    print("  artefact and it should show up here as a low win rate.")
    print()

    wins = 0
    lifts: list[int] = []
    for t in range(args.trials):
        rng = random.Random(1000 + t)
        table = perturbed_table(rng, magnitude=0.30)
        cases = generate(args.n, seed=args.seed + t, now=NOW)

        import rre.outcomes as outcomes_mod

        original = outcomes_mod.RECOVERY_PROBABILITY
        outcomes_mod.RECOVERY_PROBABILITY = table
        try:
            run_agent(cases, reasoner=OfflineReasoner(now=NOW), now=NOW, seed=args.seed)
            agent = score_agent(cases)
            n_out = naive_retry(cases, seed=args.seed)
            naive = score_baseline("naive", cases, n_out, naive_cost(cases, n_out))
        finally:
            outcomes_mod.RECOVERY_PROBABILITY = original

        lift = agent.net_paise - naive.net_paise
        lifts.append(lift)
        if lift > 0:
            wins += 1
        print(f"  trial {t + 1:2d}   net lift {fmt_inr(lift):>18}   {'win' if lift > 0 else 'LOSS'}")

    print()
    print(f"  agent beat naive retry in {wins}/{args.trials} perturbed worlds")
    print(f"  median lift  {fmt_inr(sorted(lifts)[len(lifts) // 2])}")
    print(f"  worst case   {fmt_inr(min(lifts))}")
    print()
    return 0


def cmd_audit(args: argparse.Namespace) -> int:
    """Show what the audit trail actually records, including the refusals."""
    import json

    path = REPORTS / "audit.jsonl"
    if not path.exists():
        print("No audit trail yet. Run `python -m rre demo` first.")
        return 1

    entries = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    blocked = [e for e in entries if not e["policy"]["allowed"]]

    print()
    print(f"  {len(entries)} decisions logged, {len(blocked)} of them refusals.")
    print()
    print(_rule("="))
    print("  SAMPLE: actions taken")
    print(_rule("="))
    for e in [x for x in entries if x["policy"]["allowed"]][: args.n]:
        print(json.dumps(e, indent=2, ensure_ascii=False))
        print()

    print(_rule("="))
    print("  SAMPLE: actions refused  (the entries most systems never write)")
    print(_rule("="))
    for e in blocked[: args.n]:
        print(json.dumps(e, indent=2, ensure_ascii=False))
        print()
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    return cmd_demo(args)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="rre", description="Bounded, auditable revenue recovery agent."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("demo", help="run the full benchmark")
    p.add_argument("-n", type=int, default=400, help="batch size")
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--progress", action="store_true")
    p.set_defaults(func=cmd_demo)

    p = sub.add_parser("ablation", help="LLM vs deterministic rules")
    p.add_argument("-n", type=int, default=400)
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--llm", action="store_true", help="include the Claude arm")
    p.set_defaults(func=cmd_ablation)

    p = sub.add_parser("sensitivity", help="perturb the outcome table")
    p.add_argument("-n", type=int, default=400)
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--trials", type=int, default=10)
    p.set_defaults(func=cmd_sensitivity)

    p = sub.add_parser("audit", help="inspect the audit trail")
    p.add_argument("-n", type=int, default=2, help="samples per section")
    p.set_defaults(func=cmd_audit)

    p = sub.add_parser("report", help="write the HTML report")
    p.add_argument("-n", type=int, default=400)
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--progress", action="store_true")
    p.set_defaults(func=cmd_report)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
