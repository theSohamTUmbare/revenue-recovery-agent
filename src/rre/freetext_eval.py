"""A fair fight: free-text intent extraction.

**Why this file exists.**

The headline ablation (``make ablation``) reports the deterministic rules beating
the LLM on root-cause accuracy, 87.5% to 70.1%. Taken at face value that says
the LLM is decoration and should be deleted.

Taken at face value it would be wrong, and the reason is a flaw in our own
benchmark. ``OfflineReasoner._classify`` is very nearly the inverse of
``generate._pick_case_shape``. The same person wrote both, one after the other.
The generator picks a cause and *then* samples a symptom conditional on it; the
rules invert that mapping. It is not a baseline, it is the answer key with extra
steps, and no rules engine deployed against real traffic would have that.

So the root-cause ablation measures the benchmark, not the world, and we say so
in the README rather than quoting the 87.5% as if we had earned it.

**What this file measures instead.** The one sub-problem where the rules have no
insider knowledge: reading what a customer actually wrote. The generator emits
free-text replies in English, Hindi and Hinglish. Nothing about the phrasing was
reverse-engineered into the regexes -- they are an honest best effort at the
patterns a developer would think to write.

Two things are scored, and the first matters more than any accuracy number in
this repository:

1. **Opt-out and dispute detection.** Missing an opt-out means messaging someone
   who told you to stop. This is the safety-critical extraction in the whole
   system, and it runs on prose.
2. **Promise-to-pay date extraction.** "5 tarikh ko", "by Friday", "month end",
   "2 din me" -- dates expressed the way people actually express them.

Run it with ``python -m rre freetext``. 15 calls, so it stays inside a free tier.
"""

from __future__ import annotations

from dataclasses import dataclass

from .generate import CUSTOMER_REPLIES

#: Hand-labelled ground truth for every reply the generator can emit.
#:
#: ``intent`` is the safety-critical label. ``has_promise`` marks replies that
#: commit to paying by some identifiable time. Labels were written by reading
#: the strings, before either reasoner was run against them.
LABELS: dict[str, tuple[str | None, bool]] = {
    "bhai salary 5 tarikh ko aa rahi hai, uske baad kar dunga payment": ("will_pay", True),
    "Sorry, was travelling. Will clear this by Friday for sure.": ("will_pay", True),
    "card change ho gaya hai, naya wala update karna padega": ("broken_instrument", False),
    "STOP. Do not message me again.": ("opt_out", False),
    "Please remove me from your list, I have already cancelled.": ("opt_out", False),
    "paisa kat gaya but order show nahi ho raha, kya scene hai?": ("dispute", False),
    "I was charged twice for this. Raising it with my bank.": ("dispute", False),
    "Bank ka server down tha kal, aaj try karta hoon": (None, False),
    "account me balance nahi tha, 2 din me daal dunga": ("will_pay", True),
    "Invoice is with our finance team, payment run happens on the 10th.": ("will_pay", True),
    "net banking kaam nahi kar raha, UPI se kar sakta hoon?": (None, False),
    "Why do you keep messaging me? I paid this last week.": ("dispute", False),
    "thoda time chahiye, month end tak clear kar dunga": ("will_pay", True),
    "My card expired last month, send me a link to update it.": ("broken_instrument", False),
    "OTP hi nahi aaya teen baar, phir chhod diya": (None, False),
}

#: Intents where a miss puts a message in front of someone who said don't.
SAFETY_CRITICAL = {"opt_out", "dispute"}


@dataclass
class FreetextScore:
    reasoner: str
    n: int = 0
    intent_correct: int = 0
    promise_correct: int = 0
    #: The number that matters: opt-outs and disputes the reasoner failed to see.
    safety_misses: int = 0
    safety_total: int = 0
    errors: int = 0
    misses: list[str] | None = None

    @property
    def intent_accuracy(self) -> float:
        return self.intent_correct / self.n if self.n else 0.0

    @property
    def promise_accuracy(self) -> float:
        return self.promise_correct / self.n if self.n else 0.0

    @property
    def safety_recall(self) -> float:
        hit = self.safety_total - self.safety_misses
        return hit / self.safety_total if self.safety_total else 0.0


def evaluate(reasoner, *, base_evidence: dict | None = None) -> FreetextScore:
    """Score one reasoner on intent and promise extraction from prose."""
    score = FreetextScore(reasoner=getattr(reasoner, "name", "?"), misses=[])

    for reply, (true_intent, has_promise) in LABELS.items():
        evidence = dict(base_evidence or {})
        evidence.update(
            {
                "case_id": "freetext_eval",
                "amount": "Rs 5,000.00",
                "rail": "card",
                "failure_code": "insufficient_funds",
                "attempt_number": 1,
                "prior_failures": [],
                "issuer_bank": "HDFC",
                "issuer_failure_rate_1h": 0.02,
                "rail_failure_rate_1h": 0.01,
                "days_overdue": 0,
                "customer_reply": reply,
                "is_subscription": False,
                "occurred_at": "2026-09-05T14:30:00+00:00",
            }
        )

        result = reasoner.diagnose(evidence)
        score.n += 1

        if result.source == "llm-error":
            score.errors += 1
            continue

        got_intent = result.customer_intent
        # Credit an exact match, or a correct "nothing notable here".
        if got_intent == true_intent or (got_intent is None and true_intent is None):
            score.intent_correct += 1

        if true_intent in SAFETY_CRITICAL:
            score.safety_total += 1
            if got_intent != true_intent:
                score.safety_misses += 1
                assert score.misses is not None
                score.misses.append(
                    f"[{true_intent} -> {got_intent}] {reply[:60]}"
                )

        got_promise = result.promise_to_pay_date is not None
        if got_promise == has_promise:
            score.promise_correct += 1

    return score


def rows(scores: list[FreetextScore]) -> list[dict[str, str]]:
    out = []
    for s in scores:
        out.append(
            {
                "reasoner": s.reasoner,
                "intent": f"{s.intent_accuracy:.0%}",
                "promise date": f"{s.promise_accuracy:.0%}",
                "opt-out/dispute recall": f"{s.safety_recall:.0%}"
                + f" ({s.safety_total - s.safety_misses}/{s.safety_total})",
                "errors": str(s.errors),
            }
        )
    return out


def check_coverage() -> None:
    """Every reply the generator can emit must carry a label.

    Guards against the eval silently shrinking when someone adds a reply
    template to the generator.
    """
    generated = {reply for reply, _ in CUSTOMER_REPLIES}
    missing = generated - set(LABELS)
    if missing:
        raise AssertionError(f"unlabelled replies in the generator: {missing}")
