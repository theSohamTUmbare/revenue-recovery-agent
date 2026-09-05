"""Self-contained HTML run report.

No CDN, no build step, no network. One file you can open, or attach to an email
to a merchant. The layout follows the argument the project makes, in order:
what was at stake, what the three arms did, how good the diagnosis actually was,
what the agent refused to do, and what it could not resolve.
"""

from __future__ import annotations

import html
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from .domain import Case, fmt_inr
from .metrics import (
    ArmMetrics,
    contacts_avoided,
    diagnosis_report,
    exception_list,
)

CSS = """
:root {
  --bg: #fbfaf8; --fg: #1c1a17; --muted: #6b665f; --line: #e3ded6;
  --card: #ffffff; --accent: #14532d; --warn: #9a3412; --bad: #991b1b;
  --good-bg: #f0fdf4; --bad-bg: #fef2f2;
}
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--fg);
  font:15px/1.6 ui-sans-serif,-apple-system,"Segoe UI",Roboto,sans-serif; }
.wrap { max-width: 980px; margin: 0 auto; padding: 40px 24px 80px; }
h1 { font-size: 30px; margin:0 0 6px; letter-spacing:-.02em; }
h2 { font-size: 19px; margin:44px 0 4px; letter-spacing:-.01em; }
h2 .n { color: var(--muted); font-weight:400; font-size:15px; }
.sub { color: var(--muted); margin:0 0 8px; }
.lede { color:var(--muted); font-size:14px; margin:0 0 18px; max-width:70ch; }
.card { background:var(--card); border:1px solid var(--line); border-radius:10px;
  padding:18px 20px; margin:14px 0; }
.grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(160px,1fr)); gap:12px; }
.stat { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:14px 16px; }
.stat .k { color:var(--muted); font-size:12px; text-transform:uppercase; letter-spacing:.05em; }
.stat .v { font-size:21px; font-weight:600; margin-top:4px; font-variant-numeric:tabular-nums; }
.stat .n { color:var(--muted); font-size:12px; margin-top:2px; }
table { border-collapse:collapse; width:100%; font-size:14px; }
th,td { text-align:left; padding:8px 10px; border-bottom:1px solid var(--line); }
th { font-size:11px; text-transform:uppercase; letter-spacing:.06em; color:var(--muted); font-weight:600; }
td.num, th.num { text-align:right; font-variant-numeric:tabular-nums; }
tr.hl td { background:var(--good-bg); font-weight:600; }
tr.bad td { background:var(--bad-bg); }
.scroll { overflow-x:auto; }
.tag { display:inline-block; font-size:11px; padding:2px 7px; border-radius:99px;
  background:#f1efe9; color:var(--muted); border:1px solid var(--line); }
.bar { height:7px; background:#eee9e1; border-radius:99px; overflow:hidden; }
.bar > i { display:block; height:100%; background:var(--accent); }
code { background:#f4f2ec; padding:1px 5px; border-radius:4px; font-size:13px; }
pre { background:#f7f5f0; border:1px solid var(--line); border-radius:8px;
  padding:12px 14px; overflow-x:auto; font-size:12.5px; line-height:1.5; }
.foot { color:var(--muted); font-size:12.5px; margin-top:50px; border-top:1px solid var(--line); padding-top:16px; }
"""


def _esc(x: Any) -> str:
    return html.escape(str(x))


def _stat(k: str, v: str, note: str = "") -> str:
    n = f'<div class="n">{_esc(note)}</div>' if note else ""
    return f'<div class="stat"><div class="k">{_esc(k)}</div><div class="v">{_esc(v)}</div>{n}</div>'


def write_html(
    *,
    cases: list[Case],
    result: Any,
    arms: list[ArmMetrics],
    summary: dict[str, Any],
    out: Path,
) -> Path:
    agent = arms[0]
    naive = arms[1]
    blast = arms[2]
    metrics, confusions = diagnosis_report(cases)
    exceptions = exception_list(cases)
    avoided = contacts_avoided(cases)
    acc = result.diagnosis_correct / max(1, result.diagnosis_total)

    arm_rows = []
    for a in arms:
        cls = "hl" if a is agent else ("bad" if a.violations else "")
        arm_rows.append(
            f'<tr class="{cls}"><td>{_esc(a.label)}</td>'
            f'<td class="num">{_esc(fmt_inr(a.gross_recovered_paise))}</td>'
            f'<td class="num">{_esc(fmt_inr(a.cost_paise))}</td>'
            f'<td class="num">{_esc(fmt_inr(a.net_paise))}</td>'
            f'<td class="num">{a.n_recovered}/{a.n_recoverable}</td>'
            f'<td class="num">{a.recovery_rate:.1%}</td>'
            f'<td class="num">{a.contacts_made}</td>'
            f'<td class="num">{a.violations}</td></tr>'
        )

    diag_rows = "".join(
        f"<tr><td>{_esc(m.cause)}</td><td class='num'>{m.support}</td>"
        f"<td class='num'>{m.precision:.2f}</td><td class='num'>{m.recall:.2f}</td>"
        f"<td class='num'>{m.f1:.2f}</td>"
        f"<td style='width:140px'><div class='bar'><i style='width:{m.f1 * 100:.0f}%'></i></div></td></tr>"
        for m in metrics
    )

    fire_rows = "".join(
        f"<tr><td>{_esc(r)}</td><td class='num'>{n}</td></tr>"
        for r, n in result.policy_rule_fires.items()
    ) or "<tr><td colspan='2'>none fired</td></tr>"

    conf_rows = "".join(
        f"<tr><td>{_esc(lbl)}</td><td class='num'>{n}</td></tr>" for lbl, n in confusions
    ) or "<tr><td colspan='2'>no confusions</td></tr>"

    exc_rows = "".join(
        f"<tr><td><code>{_esc(e['case_id'])}</code></td><td class='num'>{_esc(e['amount'])}</td>"
        f"<td>{_esc(e['outcome'])}</td><td>{_esc(e['believed_cause'])}</td>"
        f"<td class='num'>{_esc(e['confidence'])}</td><td>{_esc(e['reason'])}</td></tr>"
        for e in exceptions[:60]
    )

    blocked_samples = [e for e in result.audit.entries if not e["policy"]["allowed"]][:3]
    sample_json = "\n\n".join(
        json.dumps(e, indent=2, ensure_ascii=False) for e in blocked_samples
    )

    from .breakeven import analyse, per_case_harm

    be = analyse(agent, blast)
    if be.agent_wins_at_current_price:
        headline = "The gated agent is ahead on net, even before counting the harm"
        be_body = (
            f"<p class='lede' style='margin:0'>It leads '{_esc(blast.label)}' by "
            f"<strong>{_esc(fmt_inr(be.gap_paise))}</strong> at the current "
            f"{_esc(fmt_inr(be.current_price_paise))} violation price.</p>"
        )
    else:
        headline = "Honest result: the unrestrained baseline beats this agent on net"
        be_body = f"""
        <p class="lede" style="margin:0 0 10px">'{_esc(blast.label)}' comes out
        <strong>{_esc(fmt_inr(-be.gap_paise))}</strong> ahead. At
        {_esc(fmt_inr(be.current_price_paise))} per violation, pursuing
        {be.violations} people without a gate is profitable. We are reporting that rather
        than raising the constant until it stops being true.</p>
        <p class="lede" style="margin:0 0 10px">The two arms break even at
        <strong>{_esc(fmt_inr(be.breakeven_price_paise or 0))} per violation</strong>. That is the
        assumption the whole comparison rests on, so it is stated instead of buried: a reader can
        decide for themselves whether one unwanted message about someone's money costs a merchant
        more or less than that, once a support ticket, a chargeback and a churn event are counted.</p>
        <p class="lede" style="margin:0">We would still not ship the aggressive arm, and the reason is
        not arithmetic. A cost model can express &ldquo;this is expensive&rdquo;. It cannot express
        &ldquo;this is not ours to do&rdquo;.</p>"""

    harm_rows = "".join(
        f"<tr><td>{_esc(k)}</td><td class='num'>{n}</td><td style='font-size:13px;color:var(--muted)'>{_esc(o)}</td></tr>"
        for k, n, o in per_case_harm(blast, len(cases))
    ) or "<tr><td colspan='3'>none</td></tr>"

    avoided_rows = "".join(
        f"<tr><td>{_esc(k)}</td><td class='num'>{v}</td></tr>" for k, v in avoided.items()
    ) or "<tr><td colspan='2'>none</td></tr>"

    usage = ""
    if result.llm_usage:
        u = result.llm_usage
        usage = f"""
        <h2>What the AI cost <span class="n">token accounting</span></h2>
        <p class="lede">Reported next to the money so the trade is visible rather than assumed.</p>
        <div class="grid">
          {_stat("Model calls", f"{u.get('calls', 0):,}")}
          {_stat("Input tokens", f"{u.get('input_tokens', 0):,}")}
          {_stat("Cache reads", f"{u.get('cache_read_tokens', 0):,}", "shared system prompt")}
          {_stat("Output tokens", f"{u.get('output_tokens', 0):,}")}
        </div>"""

    doc = f"""<title>Recovery Agent Run Report</title>
<style>{CSS}</style>
<div class="wrap">
  <h1>Revenue recovery agent &mdash; run report</h1>
  <p class="sub">{summary['n_cases']} at-risk payments &middot; {summary['n_customers']} customers &middot;
     reasoner: <span class="tag">{_esc(result.reasoner_name)}</span> &middot;
     generated {datetime.now().strftime('%Y-%m-%d %H:%M')}</p>

  <h2>What was at stake</h2>
  <div class="grid">
    {_stat("Total at risk", fmt_inr(summary['total_at_risk_paise']))}
    {_stat("Actually recoverable", fmt_inr(summary['theoretical_max_recoverable_paise']), "nobody can beat this ceiling")}
    {_stat("Opted out", str(summary['n_opted_out']), "must never be contacted")}
    {_stat("Open disputes", str(summary['n_disputes']), "must not be chased")}
    {_stat("Promised to pay", str(summary['n_with_promise']), "must be left alone until the date")}
  </div>

  <h2>Three arms, one batch <span class="n">identical cases, identical outcome model</span></h2>
  <p class="lede">The aggressive arm is the important comparison. It recovers more gross rupees than
  the agent by pursuing everyone without a gate &mdash; and ends up behind on net once the cost of
  doing that to people is counted.</p>
  <div class="card scroll">
    <table>
      <tr><th>Arm</th><th class="num">Gross</th><th class="num">Cost</th><th class="num">Net</th>
          <th class="num">Recovered</th><th class="num">Rate</th><th class="num">Contacts</th>
          <th class="num">Violations</th></tr>
      {''.join(arm_rows)}
    </table>
  </div>
  <div class="grid">
    {_stat("Net lift vs naive retry", fmt_inr(agent.net_paise - naive.net_paise))}
    {_stat("Contacts to earn it", f"{agent.contacts_made}", f"the blast arm needed {blast.contacts_made}")}
    {_stat("People the blast arm violated", f"{blast.violations}", f"{blast.violations / max(1, len(cases)):.0%} of the batch")}
    {_stat("Agent violations", f"{agent.violations}", "by construction")}
  </div>

  <div class="card" style="border-left:3px solid var(--warn)">
    <h3 style="margin:0 0 8px;font-size:16px">{_esc(headline)}</h3>
    {be_body}
  </div>

  <div class="card scroll">
    <table><tr><th>What the aggressive arm did to earn that</th><th class="num">Count</th><th>What it actually breaches</th></tr>{harm_rows}</table>
  </div>

  <h2>Diagnosis quality <span class="n">vs held-out ground truth</span></h2>
  <p class="lede">Per-class, because a single accuracy number hides the failure that matters:
  mistaking a hard decline for insufficient funds sends a real person a message about money that
  was never going to be charged.</p>
  <div class="grid">{_stat("Overall accuracy", f"{acc:.1%}", f"{result.diagnosis_correct}/{result.diagnosis_total} cases")}</div>
  <div class="card scroll">
    <table>
      <tr><th>Root cause</th><th class="num">n</th><th class="num">Precision</th>
          <th class="num">Recall</th><th class="num">F1</th><th></th></tr>
      {diag_rows}
    </table>
  </div>
  <div class="card scroll">
    <table><tr><th>Most frequent confusion</th><th class="num">Count</th></tr>{conf_rows}</table>
  </div>

  <h2>Restraint <span class="n">what the gate stopped</span></h2>
  <p class="lede">Every rule that fired, by name. These are the decisions most recovery tooling
  never records, because from the outside nothing happened.</p>
  <div class="grid">
    {_stat("Contacts refused", str(sum(avoided.values())), "playbook wanted to send them")}
    {_stat("Agent violations", str(agent.violations), "by construction, not by luck")}
  </div>
  <div class="card scroll">
    <table><tr><th>Policy rule</th><th class="num">Times fired</th></tr>{fire_rows}</table>
  </div>
  <div class="card scroll">
    <table><tr><th>Contact blocked by</th><th class="num">Count</th></tr>{avoided_rows}</table>
  </div>

  <h2>The audit trail <span class="n">sample refusals</span></h2>
  <p class="lede">A refusal carries the same weight as an action: what we believed, what we wanted
  to do, which rule stopped us, and whether it could be undone.</p>
  <pre>{_esc(sample_json)}</pre>

  <h2>Exception list <span class="n">{len(exceptions)} cases not resolved automatically</span></h2>
  <p class="lede">The list a merchant's ops team would work from tomorrow morning. Publishing it is
  what makes the headline number believable &mdash; &ldquo;it handled everything&rdquo; is never true.</p>
  <div class="card scroll">
    <table>
      <tr><th>Case</th><th class="num">Amount</th><th>Outcome</th><th>Believed cause</th>
          <th class="num">Conf.</th><th>Reason</th></tr>
      {exc_rows}
    </table>
    {f'<p class="sub">Showing 60 of {len(exceptions)}.</p>' if len(exceptions) > 60 else ''}
  </div>
  {usage}

  <div class="foot">
    Every figure here comes from the pre-registered outcome table in
    <code>src/rre/outcomes.py</code>, which was written before the agent and is applied identically
    to all three arms. It encodes documented payments behaviour, not measurements from a real
    Razorpay dataset. Run <code>python -m rre sensitivity</code> to see whether the conclusion
    survives shaking those numbers by &plusmn;30%.
  </div>
</div>"""

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(doc, encoding="utf-8")
    return out
