# Buildathon form — prepared answers

The form asks for 12 things. Six are yours to fill in; the other six are below,
ready to paste.

## Yours to fill

| Field | Notes |
|---|---|
| Full name | |
| College | |
| Graduation year | |
| In-person from September | yes / no |
| 6 or 12 months | your pick |
| Resume file | they still take it, they just don't screen on it |

## Track

**03 — AI Revenue Recovery**

## Project name

**Recovery agent that refuses to harass**

Alternatives if you'd rather something drier: *Bounded Revenue Recovery Agent*,
or *Gated Recovery Agent*.

## What it solves

> Failed payments, abandoned checkouts and overdue invoices all arrive looking
> the same, and most recovery tooling responds to all of them the same way: retry
> three times, then email. Retrying an expired card is three fees for guaranteed
> nothing; the email goes out whether or not the customer asked you to stop.
>
> This agent diagnoses *why* revenue is at risk — eight root causes from
> ambiguous evidence, where the same failure code can mean opposite things — then
> applies the intervention that actually fixes that cause, then passes it through
> a deterministic policy gate that can refuse. Across 400 synthetic cases it nets
> ₹38.3L against ₹16.4L for a standard retry loop, using 66 customer contacts
> instead of 400 and zero prohibited ones. Every decision is auditable, including
> the refusals.

## GitHub repo

https://github.com/theSohamTUmbare/revenue-recovery-agent

**Confirm it's public before submitting.**

## 5-min pitch video

See [pitch-script.md](pitch-script.md). Unlisted YouTube is fine.

## What broke, and how you got out

They read this one first. Long enough to be real, short enough to finish.

> Three things, and the second is the one I'd want you to read.
>
> **The benchmark came out against me.** I built an aggressive baseline that
> retries everything and messages everyone, expecting to beat it. It beat me — by
> ₹5L. One constant in my cost model (what an unwanted message costs) would have
> flipped it, and I'd set that constant myself. I left it alone and made the
> benchmark print the break-even instead: ₹2,927 per violation. Above that,
> restraint pays for itself; below it, harassment is profitable and I'm asking
> you to value something the numbers don't. The reason I'd still ship the gated
> version is regulatory, not arithmetic — messaging someone after they withdraw
> consent is a DPDP breach, not a line item. Publishing that made the project
> better: "restraint wins on rupees" was a weaker and more fragile claim than
> "here's exactly what restraint costs, and here's why I'd pay it."
>
> **My agent fabricated a measurement.** First live run against a real model, the
> free-tier quota ran out mid-batch. 384 of 400 calls failed — and my error
> handler returned `hard_decline` as the fallback, so every failure entered the
> confusion matrix as a real prediction. The run cheerfully reported "11.2%
> accuracy". A system that couldn't reach the model produced a number anyway,
> which is exactly the failure the whole project claims to prevent, sitting in my
> own code. Failed diagnoses are now `root_cause=None`: excluded from every
> accuracy figure, counted separately, escalated to a human. I also added a
> token-bucket rate limiter, because eight worker threads drain a per-minute
> quota in about three seconds.
>
> **My ablation was rigged and I didn't notice.** I measured whether the LLM
> earned its place. The deterministic rules beat it, 87.5% to 70.1% — so I nearly
> concluded the AI was decoration. Then I looked at why: I'd written both the
> rules and the data generator, and the rules were close to the generator
> inverted. Not a baseline, the answer key with extra steps. So I built the one
> comparison it couldn't rig — reading actual customer prose, where the regexes
> have no insider knowledge. There the model wins 100% to 80% on opt-out and
> dispute recall. The regex missed *"Why do you keep messaging me? I paid this
> last week."* — no keyword fires, and it's unmistakably someone telling you to
> stop. I report both numbers and claim only the second.

---

## Before you submit

- [ ] Repo is **public**
- [ ] `make demo` works on a fresh clone with no API key
- [ ] Video is uploaded and the link is unlisted-but-viewable
- [ ] **Rotate the Gemini API key** — it was pasted in plaintext
- [ ] README's `<this repo>` clone placeholder replaced with the real URL
