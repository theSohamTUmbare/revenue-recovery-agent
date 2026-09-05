"""Structural tests for the claims the README makes.

These are the tests that keep the project honest as it changes. Each one
enforces a sentence written elsewhere in the repo, so that sentence cannot
quietly stop being true.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "rre"

#: Modules that make up the agent's decision path. None of them may read ground
#: truth or the outcome probabilities.
# orchestrator.py is deliberately absent: it IS the wall. It runs the agent and
# then plays the world resolving outcomes, so it necessarily touches both sides.
# The wall it enforces is tested behaviourally instead, in
# test_reasoner_only_ever_sees_evidence below.
AGENT_MODULES = ["llm.py", "intervene.py", "policy.py"]

#: Fields that exist only on the harness side of the wall.
TRUTH_FIELDS = {"true_root_cause", "recoverable"}


def _names_used(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            names.add(node.value)
    return names


@pytest.mark.parametrize("module", AGENT_MODULES)
def test_agent_never_reads_ground_truth(module: str) -> None:
    """The agent must infer the cause, not look it up.

    Enforces the claim in domain.py: 'Nothing in the agent pipeline may read
    true_root_cause.' Without this test that promise is just a comment.
    """
    path = SRC / module
    if not path.exists():
        pytest.skip(f"{module} not present")
    used = _names_used(path)
    leaked = used & TRUTH_FIELDS
    assert not leaked, (
        f"{module} references ground-truth field(s) {leaked}. The agent would be "
        "cheating: it must infer the root cause from Signal evidence alone."
    )


@pytest.mark.parametrize("module", AGENT_MODULES)
def test_agent_never_imports_the_outcome_table(module: str) -> None:
    """The agent must not see the probabilities it is being scored against.

    If the decision path could read RECOVERY_PROBABILITY it could pick the
    argmax directly, and every reported number would be meaningless.
    """
    path = SRC / module
    if not path.exists():
        pytest.skip(f"{module} not present")
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and "outcomes" in node.module:
            pytest.fail(
                f"{module} imports from outcomes.py. The agent must not see the "
                "world model it is scored against."
            )
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert "outcomes" not in alias.name, f"{module} imports outcomes"


def test_policy_does_not_import_llm() -> None:
    """The guardrails must not depend on a model.

    This is the load-bearing claim of the whole project: no model output decides
    whether a human is contacted. It is enforced here rather than asserted in
    prose.
    """
    source = (SRC / "policy.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            assert "llm" not in node.module, (
                "policy.py imports from llm.py. The policy gate must remain "
                "deterministic and model-free."
            )


def test_outcome_table_predates_the_agent() -> None:
    """Every root cause has a full row, so no intervention is silently unscored."""
    from rre.domain import Intervention, RootCause
    from rre.outcomes import RECOVERY_PROBABILITY

    for cause in RootCause:
        assert cause in RECOVERY_PROBABILITY, f"{cause} missing from the outcome table"
        row = RECOVERY_PROBABILITY[cause]
        for iv in Intervention:
            assert iv in row, f"{cause} has no probability for {iv}"
            assert 0.0 <= row[iv] <= 1.0, f"{cause}/{iv} is not a probability"


def test_signal_evidence_carries_no_truth() -> None:
    """The payload handed to the reasoner must be free of ground truth."""
    from rre.generate import generate

    for case in generate(50, seed=3):
        evidence = case.signal.to_evidence()
        assert not (set(evidence) & TRUTH_FIELDS), (
            f"Signal.to_evidence leaked {set(evidence) & TRUTH_FIELDS}"
        )


def test_generator_is_deterministic() -> None:
    """Reported numbers are only checkable if the batch is reproducible."""
    from rre.generate import generate

    a = generate(200, seed=42)
    b = generate(200, seed=42)
    assert [c.signal.case_id for c in a] == [c.signal.case_id for c in b]
    assert [c.signal.amount_paise for c in a] == [c.signal.amount_paise for c in b]
    assert [c.true_root_cause for c in a] == [c.true_root_cause for c in b]


def test_money_is_never_float() -> None:
    """Rupee values stay integer paise all the way through."""
    from rre.generate import generate

    for case in generate(100, seed=5):
        assert isinstance(case.signal.amount_paise, int)


def test_reasoner_only_ever_sees_evidence() -> None:
    """The strongest form of the no-cheating claim: watch what the reasoner is handed.

    A syntactic scan can be fooled by indirection. This spies on every payload
    that actually reaches the reasoner during a real run and asserts that none of
    them carries ground truth, and that each is exactly the whitelisted evidence
    dict rather than anything richer.
    """
    from rre.generate import generate
    from rre.llm import OfflineReasoner, ReasonerResult
    from rre.orchestrator import run_agent

    seen: list[dict] = []

    class SpyReasoner(OfflineReasoner):
        name = "spy"

        def diagnose(self, evidence: dict) -> ReasonerResult:
            seen.append(evidence)
            return super().diagnose(evidence)

    cases = generate(60, seed=9)
    allowed_keys = set(cases[0].signal.to_evidence())
    run_agent(cases, reasoner=SpyReasoner(), max_workers=2)

    assert len(seen) == 60, "every case should reach the reasoner exactly once"
    for payload in seen:
        assert not (set(payload) & TRUTH_FIELDS), f"ground truth leaked: {payload}"
        assert set(payload) <= allowed_keys, (
            f"reasoner received unexpected keys: {set(payload) - allowed_keys}"
        )
