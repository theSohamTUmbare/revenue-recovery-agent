# A revenue recovery agent that refuses to harass

**Track 03 — AI Revenue Recovery · Razorpay AI Buildathon 2026**

Most revenue recovery tooling is a retry loop with a mailing list bolted on. It
retries the expired card three times, messages the customer who already opted
out, and reports the money it clawed back without mentioning what it spent to
get there.

This is an agent that detects revenue at risk, works out *why* it is at risk,
picks the intervention that actually fixes that specific cause, and then passes
the whole thing through a gate that can say no. It recovers **₹38.3 lakh of a
₹97 lakh recoverable ceiling** across a 400-case batch, a **₹21.9 lakh net lift
over the retry loop most merchants run today** — while making 66 customer
contacts instead of 400, and zero prohibited ones.

```bash
git clone https://github.com/theSohamTUmbare/revenue-recovery-agent
cd revenue-recovery-agent
make demo
```

No API key. No install. No network. Runs on a bare Python 3.11 in about two
seconds and prints the full benchmark. Set `GEMINI_API_KEY` (or
`ANTHROPIC_API_KEY`) and the same command runs diagnosis through a model
instead — see [Running with and without a key](#running-with-and-without-a-key).

---

## The headline result

400 synthetic at-risk payments. ₹1.18 crore at risk, of which ₹97 lakh is
actually recoverable by anybody. Three arms, same batch, same outcome model.

| Arm | Gross | Cost | Net | Rate | Contacts | Violations |
|---|---:|---:|---:|---:|---:|---:|
| **policy-gated agent** | ₹38,31,652 | ₹5,433 | **₹38,26,218** | 44.8% | **66** | **0** |
| naive retry ×3 | ₹16,38,943 | ₹2,720 | ₹16,36,223 | 35.7% | 0 | 0 |
| blast everyone | ₹44,38,362 | ₹1,07,160 | ₹43,31,202 | 46.9% | 400 | 208 |

Diagnosis accuracy against held-out ground truth: **88.5%**, with per-cause
precision and recall in the run report rather than a single flattering number.

### The result we did not want

Read the third row again. **The unrestrained baseline beats this agent on net
rupees.** It recovers ₹5.05 lakh more by retrying everything and messaging
everyone — including 50 customers who opted out, 49 with open disputes, and 116
already past their weekly contact cap.

The easy fix was to raise `COST_PER_VIOLATION_PAISE` until the agent won. We
didn't. The constant is still ₹500, and instead the benchmark reports the
**break-even price: ₹2,927 per violation.** Above that, restraint pays for
itself on rupees alone; below it, harassment is profitable and we are asking you
to value something the spreadsheet doesn't.

That number is the assumption the entire comparison rests on, so it is stated
rather than buried. Decide for yourself whether one unwanted message about
someone's money costs a merchant more or less than ₹2,927 once a support ticket,
a chargeback, an unsubscribe and a churn event are counted.

We would still not ship the aggressive arm, and the reason is not arithmetic.
A cost model can express *this is expensive*. It cannot express *this is not
ours to do*. Messaging someone after they withdraw consent isn't a line item —
under the DPDP Act 2023 it's a compliance breach, and TRAI's UCC rules treat
repeat unsolicited commercial communication as grounds for disconnecting the
sender. The full mapping from each violation to the obligation it breaches is in
[`src/rre/breakeven.py`](src/rre/breakeven.py).

---

## How it works

```
                    ┌──────────────────────────────────────────┐
   at-risk payment  │  evidence only — no ground truth         │
   ───────────────▶ │  failure code · issuer & rail telemetry  │
                    │  attempt history · free-text reply       │
                    └────────────────────┬─────────────────────┘
                                         │
                         ┌───────────────▼───────────────┐
                         │  DIAGNOSE             (LLM)   │   parallel
                         │  which of 8 root causes?      │   stateless
                         │  + parse Hinglish replies     │
                         └───────────────┬───────────────┘
                                         │ belief + confidence
                         ┌───────────────▼───────────────┐
                         │  PLAYBOOK      (lookup table) │
                         │  cause ⟶ the fix that works   │
                         └───────────────┬───────────────┘
                                         │ proposed action
        ╔════════════════════════════════▼════════════════════════════════╗
        ║  POLICY GATE            PURE CODE — NO MODEL IN THE LOOP        ║   sequential
        ║  opt-out · disputes · promise-to-pay · weekly cap · cooloff     ║   stateful
        ║  quiet hours · channel consent · futile-retry · confidence floor║   ledger
        ╚════════════════════════════════┬════════════════════════════════╝
                        allow · block · substitute
                                         │
                         ┌───────────────▼───────────────┐
                         │  EXECUTE  ·  AUDIT  ·  SCORE  │
                         └───────────────────────────────┘
```

**Eight root causes, and the codes don't tell you which.** `do_not_honour` is
what Indian issuers return for *both* a genuine risk decline and a plain empty
account — same code, opposite correct action. `gateway_timeout` means an outage,
but whether it's the *issuer* or the *rail* decides whether you wait or reroute,
and the only way to tell is the fleet-wide failure rates. That ambiguity is
deliberate: if the failure code mapped cleanly to the cause, "diagnosis" would
be a dict lookup and there'd be nothing here worth building.

**The fix follows from the cause.** Wait out an issuer outage. Reroute around a
degraded rail. Retry an empty account near payday, not immediately. Ask for a
new instrument when the card is dead — never retry it. Escalate a large hard
decline to a human rather than writing it off silently.

**The gate can say no, and can say *later*.** Three verdict shapes: allow,
block, and substitute. Quiet hours don't cancel a nudge, they move it to 09:00.
Low confidence doesn't cancel a recovery, it routes it to a person. A gate that
only knew yes and no would throw away good recoveries to avoid the 1am SMS.

---

## Where we chose *not* to use AI

This is the part we'd most like you to read, because it's the decision the
architecture is actually built around.

**An LLM reads the Hinglish SMS. An LLM does not decide whether to send one.**

`policy.py` imports nothing from `llm.py`, and
[a test enforces that](tests/test_no_leak.py) rather than a comment promising it.
Every rule about whether a human may be contacted is plain, boring, readable
Python with a name attached.

The split follows from what kind of question each one is:

| Question | Answered by | Why |
|---|---|---|
| *"bhai salary 5 tarikh ko aayegi, tab kar dunga"* — is that a payment promise, and for when? | **LLM** | Code-mixed prose. Measured: 100% vs 80% for regex, and the regex missed a dispute outright |
| *"Why do you keep messaging me? I paid this last week."* — should we contact them again? | **LLM** | No keyword fires, yet the intent is unmistakable. This is the miss that matters |
| Does `do_not_honour` here mean risk, or an empty account? | **Undecided — see below** | We expected the model to win here. On our benchmark it doesn't, and our benchmark is confounded, so we claim nothing |
| May we message this person a third time this week? | **Plain code** | Has a correct answer that must not vary with temperature, prompt wording, or model version |
| Is retrying an expired card worth it? | **Plain code** | Settled domain knowledge, belongs in a table you can diff |
| What text does the customer receive? | **Templates** | Real copy about real money — compliance should review it in advance, and it must not hallucinate an amount |

The customer-facing message is templated, not generated. It's the one thing in
the system a real person actually reads, and "the model usually gets it right"
is not a standard you can hold copy about someone's debt to.

### Does the LLM earn its place? We measured, and the first answer was no

`make ablation` runs the identical batch through both reasoners. On root-cause
classification, **the deterministic rules beat the model**:

| Reasoner | Answered | Root-cause accuracy |
|---|---|---|
| deterministic rules | 120/120 | **87.5%** |
| Gemini 3.5 Flash Lite | 107/120 | 70.1% |

Taken at face value, that says delete the LLM.

**Taken at face value it would be wrong, and the fault is ours.**
`OfflineReasoner._classify` is very nearly the *inverse* of
`generate._pick_case_shape`. The generator picks a root cause and then samples a
symptom conditional on it; the rules invert that exact mapping. The same person
wrote both, one after the other. That isn't a baseline — it's the answer key
with extra steps, and no rules engine meeting real traffic would have it.

So the root-cause ablation measures our benchmark, not the world. We're
reporting the 87.5% rather than quietly dropping a comparison that embarrasses
the model, but we're not claiming we earned it.

### The comparison the rules can't rig

`make freetext` scores the one sub-problem where the regexes have no insider
knowledge: reading what a customer actually wrote. Nothing about the phrasing
was reverse-engineered into them.

| Reasoner | Intent | Promise date | **Opt-out / dispute recall** |
|---|---:|---:|---:|
| deterministic rules | 47% | 80% | **80%** (4/5) |
| Gemini 3.5 Flash Lite | **87%** | **100%** | **100%** (5/5) |

The rules missed this one:

> *"Why do you keep messaging me? I paid this last week."*

No keyword fires. No "stop", no "unsubscribe". It is unmistakably someone
telling you to leave them alone, and the regex sees nothing — so the pipeline
would have messaged them again. That single miss is worth more than the 17-point
root-cause gap, because the root-cause gap is an artefact and this one isn't.

**The honest conclusion:** the LLM's measured value here is *reading prose* —
intent and commitments buried in code-mixed Hinglish — not classifying
structured telemetry. On telemetry we have no valid evidence either way, and we
are not going to manufacture some by tuning against a generator we wrote. The
next step is a real dataset, not more synthetic hill-climbing.

---

## How we kept ourselves honest

The failure mode for a project like this is grading your own homework. Five
things guard against it, and all five are runnable.

**1. The outcome model was written before the agent.** `git log` shows
[`outcomes.py`](src/rre/outcomes.py) landing in the first commit, ahead of
`diagnose`, `intervene` and `policy`. The world was fixed before the thing being
measured against it existed. It's also the first file worth attacking — if you
want to break this project, break that table.

**2. The agent cannot see the answer key.** Ground truth lives on `Case`; the
agent only ever receives `Signal.to_evidence()`. This is enforced two ways: an
AST scan asserting the decision path never references `true_root_cause` or
imports `outcomes.py`, and a behavioural test that spies on every payload
reaching the reasoner during a real run and asserts none carries truth.

**3. Both baselines resolve through the same function.** Whatever bias the
probability table carries, it carries equally for all three arms, so the *lift*
survives disagreement about any individual number.

**4. The conclusion survives the table being wrong.** `make sensitivity` re-runs
everything with every probability jittered ±30%:

```
agent beat naive retry in 9/10 perturbed worlds
median lift  ₹22,54,407
worst case      -₹2,466      ← it does lose sometimes, and we print that
```

**5. Nothing claims to have handled everything.** Every run publishes an
exception list — 209 of 400 cases the agent declined to resolve automatically,
each with the rule that stopped it. That's the list a merchant's ops team would
actually work from on Monday, and publishing it is what makes the headline
number believable.

---

## The audit trail

Every decision is one JSON line, **including the refusals** — the entries most
recovery tooling never writes, because from the outside nothing happened.

```json
{
  "case_id": "case_0043",
  "belief": {
    "root_cause": "insufficient_funds",
    "confidence": 0.71,
    "reasoning": "explicit no-funds code with healthy issuer telemetry",
    "reasoner": "deterministic-fallback"
  },
  "proposed": "nudge_customer",
  "final": "stop",
  "changed_by_policy": true,
  "policy": {
    "allowed": false,
    "rules_fired": ["promise_to_pay_hold"],
    "notes": ["customer committed to pay by 2026-09-12; no contact until 2026-09-13"]
  },
  "rollback": "no action taken, nothing to reverse"
}
```

`rollback` is not decoration. A scheduled retry is cancellable; **a sent message
is not**, and the log says so plainly rather than implying everything is
reversible. That asymmetry is exactly why the contact caps are enforced *before*
sending.

Run `make audit` to see real entries from both halves.

---

## Running with and without a key

| | Reasoner | What runs |
|---|---|---|
| `make demo` | deterministic rules | Everything. Full benchmark, all metrics, HTML report. |
| `GEMINI_API_KEY=... make demo` | Gemini (`gemini-3.5-flash-lite`) | Same, with model diagnosis |
| `ANTHROPIC_API_KEY=... make demo` | Claude (`claude-opus-5`) | Same, via the Anthropic path |

Two providers sit behind one `Reasoner` protocol. Adding the second one required
edits to exactly one class — the policy gate, playbook, audit trail and scoring
never learned a provider existed. That was the test of whether the seam was real
rather than decorative.

**The Anthropic path is written but unverified.** No key was available on the
machine this was built on, so every number in this README comes from either the
deterministic arm or Gemini. The Claude code is written against current API docs
and is structurally identical to the working Gemini path, but *written* and
*verified* are different claims and we are only making the first.

The fallback isn't a stub — it's a real attempt at the problem using the same
telemetry the prompt gives Claude, and it's the control arm the ablation
measures against. It exists so a reviewer is never blocked on a key, and so the
question *"would a lookup table have done?"* has an actual answer.

Cost and quota notes, both learned the hard way:

- One call per case, `temperature=0` — diagnosis has a correct answer, and a run
  whose numbers move between invocations can't be checked by a reviewer.
- A **token-bucket rate limiter** sits below the thread pool (`RRE_LLM_RPM`,
  default 10) with jittered exponential backoff on 429s. Without it, eight
  workers drain a per-minute free-tier quota in about three seconds.
- Token accounting is printed next to the money recovered, so the trade is
  visible rather than assumed.

---

## What's real and what isn't

Stated plainly, because a reviewer shouldn't have to reverse-engineer it:

- **The data is synthetic.** 400 seeded cases, reproducible with `generate(seed=7)`.
  Modelled on real Razorpay error-code shapes, not drawn from real traffic.
- **The recovery probabilities are directional, not measured.** They encode
  documented payments behaviour — futile retries on dead cards, issuer outages
  resolving, UPI carrying less auth friction than 3DS. They are not fitted to a
  real dataset, because we don't have one. This is the project's biggest
  limitation and `make sensitivity` exists to quantify it.
- **Execution is simulated.** No live Razorpay API calls. The interfaces are
  shaped for the real ones, but nothing here has moved actual money.
- **The regulatory citations are directional.** DPDP Act 2023 and TRAI UCC
  regulations are cited to establish that certain actions aren't merely
  expensive. Nothing here is legal advice.

## What broke

**The benchmark came out against us.** The first complete run showed the
unrestrained baseline beating the gated agent on net rupees, which invalidated
the pitch as originally framed. The available moves were to reprice violations
until the answer flipped, quietly drop the aggressive baseline, or publish it.

We published it and added the break-even analysis instead. It's a better project
for it: "restraint wins on rupees" was always a weaker and more fragile claim
than "here is exactly what restraint costs, priced, and here's why we'd still
choose it." The baseline that beat us is the one we'd have been most tempted to
leave out, which is a good sign it needed to be there.

**A related bug:** the first version printed the gap as *"worse on net by:
-₹5,04,984"* — technically accurate, quietly misleading, a negative number
sitting under a label implying the opposite. That framing survived because it
was written expecting the agent to win. Now the terminal prints `HONEST RESULT:`
and names which arm won.

---

## Layout

```
src/rre/
  domain.py       types; money is integer paise everywhere
  outcomes.py     the pre-registered world model — read this first
  generate.py     seeded synthetic batches with deliberate ambiguity
  llm.py          Claude reasoner + deterministic control arm
  intervene.py    cause ⟶ fix playbook
  policy.py       the gate. no model, no imports from llm
  orchestrator.py parallel diagnosis, sequential decisions
  baselines.py    naive retry + blast everyone
  metrics.py      money, diagnosis quality, restraint
  breakeven.py    the honest result and what it costs
  freetext_eval.py the one LLM-vs-rules comparison that isn't confounded
  audit.py        append-only decision log
  report.py       self-contained HTML
tests/
  test_no_leak.py structural guards on the claims above
  test_policy.py  one test per guardrail obligation
```

```bash
make demo               # full benchmark + HTML report
make test               # 27 tests
make ablation LLM=1     # does the LLM earn its place on root cause?
make freetext LLM=1     # ...and on customer prose, where the rules can't cheat
make sensitivity        # does the conclusion survive a shaken table?
make audit              # sample audit entries, including refusals
```
