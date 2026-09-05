# 5-minute pitch — script and shot list

Target 4:30, leaving margin. The single most important structural decision: **the
failure goes in the middle, not the end.** Most pitch videos hide the part where
the numbers went against them. That part is the strongest thing here.

Record the terminal at a large font. Never read a slide aloud.

---

## 0:00 — 0:35 · The problem, concretely

> "A payment fails. What most recovery systems do next is retry it three times
> and send an email. That's it — that's the whole product.
>
> The trouble is that a failed payment isn't one thing. An expired card, a bank
> having a bad hour, and an empty account all arrive looking identical, and the
> right response to each is completely different. Retrying an expired card three
> times is three fees for guaranteed nothing. And the email goes out whether or
> not that customer told you last week to stop messaging them.
>
> I built an agent that works out *why* the payment failed, picks the fix for
> that specific cause, and then passes it through a gate that's allowed to say no."

## 0:35 — 1:20 · Run it live

Run `make demo`. Let the table appear on screen.

> "Four hundred at-risk payments. About one crore eighteen at risk, of which
> ninety-seven lakh is genuinely recoverable — some of that money was never
> coming back and I'd rather say so than quietly inflate the denominator.
>
> The agent nets thirty-eight lakh. The retry loop most merchants run today nets
> sixteen. That's a twenty-two lakh lift, and it makes sixty-six customer
> contacts to get there instead of four hundred."

Pause on the `violations` column.

> "Zero prohibited contacts. Not zero because it got lucky — zero by construction."

## 1:20 — 2:10 · **The part where it went wrong**

This is the centre of the video. Slow down here.

> "Now look at the third row, because this is where the project got interesting.
>
> I built a deliberately nasty baseline that retries everything and messages
> everyone — including the people who opted out. And it beats my agent. It
> recovers five lakh more.
>
> I could have fixed that in one line. There's a constant in my cost model for
> what an unwanted message costs you, and I'd set it at five hundred rupees. Push
> it up and my agent wins.
>
> I didn't. The constant is still five hundred, and instead the benchmark now
> prints the break-even: two thousand nine hundred and twenty-seven rupees. Above
> that, restraint pays for itself. Below it, harassment is genuinely profitable
> and I'm asking you to care about something the spreadsheet doesn't.
>
> I'd still not ship the aggressive one, and the reason isn't arithmetic.
> Messaging someone after they withdraw consent isn't a line item — it's a DPDP
> breach. A cost model can say *this is expensive*. It can't say *this isn't ours
> to do*."

## 2:10 — 3:00 · Where I chose not to use AI

Show `policy.py` on screen — scroll it so they see it's plain readable code.

> "Every rule about whether a human gets contacted is in this one file. No model
> touches it, and there's a test that fails if anyone imports the LLM into it.
>
> The split is: a language model reads the Hinglish SMS. A language model does
> not decide whether to send one. 'May we message this person a third time this
> week' has a correct answer that shouldn't change with temperature or model
> version. That belongs in code you can unit-test to a fixed point.
>
> The message the customer actually receives is templated too, not generated.
> It's the one thing a real person reads, and 'the model usually gets it right'
> isn't a standard you apply to copy about someone's debt."

## 3:00 — 4:00 · Did the AI earn its place — measured, not assumed

Run `make freetext LLM=1`.

> "I wanted to know whether the LLM was doing real work or was just there because
> it's a hackathon. So I ran it against the rules.
>
> On classifying the root cause, the rules beat the model — eighty-seven to
> seventy. And then I looked at why, and it's my fault: I wrote the rules engine
> and I wrote the data generator, and the rules are basically the generator
> inverted. It's the answer key with extra steps. That comparison measures my
> benchmark, not the world, so I report it and I don't claim it.
>
> This is the fair test — reading what the customer actually wrote, where the
> regexes have no insider knowledge. The model gets a hundred percent on
> opt-out and dispute detection. The regex gets eighty. Here's the one it missed:
>
> *'Why do you keep messaging me? I paid this last week.'*
>
> No 'stop'. No 'unsubscribe'. Nothing for a keyword to catch — and it is
> unmistakably somebody telling you to leave them alone. The regex would have
> messaged them again. That one miss is worth more than the seventeen-point gap,
> because that gap is an artefact and this isn't."

## 4:00 — 4:30 · The audit trail, and close

Run `make audit`, land on a refusal entry.

> "Every decision is logged, including the refusals — what it believed, what it
> wanted to do, which rule stopped it, and whether it could be undone. A sent
> message can't be unsent, and the log says that plainly, which is why the caps
> are enforced before sending rather than after.
>
> Two hundred and nine of the four hundred cases it refused to handle
> automatically. They're all listed with reasons. 'It handled everything' is
> never true, and I'd rather hand you the exception list than a number that
> implies otherwise.
>
> Everything's synthetic and I say so in the README. The next step isn't more
> tuning against a generator I wrote — it's real data."

---

## What to have open

1. Terminal, large font, in the repo
2. `src/rre/policy.py` — for the 2:10 section
3. `reports/report.html` in a browser — optional B-roll

## Commands, in order

```bash
make demo
make freetext LLM=1     # needs GEMINI_API_KEY
make audit
```

## Traps

- **Don't apologise for the losing benchmark.** Deliver it as a finding. The
  tone is "here's what I measured", never "unfortunately".
- **Don't oversell the money.** It's synthetic. Saying so costs nothing and
  buys credibility for everything else.
- **Don't skip the regex miss.** It's the most concrete thing in the video.
- If a live command fails, say so and move on. Given what this project is
  about, quietly cutting to a pre-recorded success would be the one genuinely
  embarrassing option.
