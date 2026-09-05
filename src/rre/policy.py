"""The policy gate. Plain, boring, deterministic code.

This module is the reason the project can claim to be responsible rather than
merely claiming to be careful.

**It imports nothing from ``llm``.** No model output reaches a decision about
whether a human is contacted. The reasoner is upstream and advisory: it says
"this looks like insufficient funds, confidence 0.71, and the customer wrote
that they will pay on the 5th". This module then decides, in code a reviewer can
read end to end in five minutes, whether anything is allowed to happen at all.

That split is deliberate and it is the answer to "where did you choose *not* to
use AI". A language model is the right tool for reading a Hinglish SMS and
inferring what broke. It is the wrong tool for deciding whether it is lawful and
decent to send someone a third message this week. The second question has a
correct answer that does not vary with temperature, prompt wording, or model
version, and it should be answered by something you can unit-test to a fixed
point. ``tests/test_policy.py`` does exactly that.

Three verdict shapes:

* **allow** -- the action proceeds as proposed.
* **block** -- the action is forbidden. ``blocked_by`` names every rule that
  fired, so the audit log records *why*, not just *no*.
* **substitute** -- the action is replaced by a safer one. Quiet hours do not
  cancel a nudge, they move it to 09:00. Low confidence does not cancel a
  recovery, it routes it to a human. Blocking outright where a safe alternative
  exists would be its own kind of failure.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta

from .domain import (
    CONTACT_INTERVENTIONS,
    Channel,
    Customer,
    Diagnosis,
    Intervention,
    PolicyConfig,
    PolicyVerdict,
    RootCause,
    Signal,
)

#: Preference order for reaching someone. Cheapest and least intrusive first;
#: voice is last because a phone call about money is the most disruptive thing
#: on this list.
CHANNEL_PREFERENCE: tuple[Channel, ...] = (
    Channel.EMAIL,
    Channel.WHATSAPP,
    Channel.SMS,
    Channel.VOICE,
)


class PolicyEngine:
    """Stateful across a batch, because the caps are cross-case by nature.

    Two customers' cases are independent; two cases belonging to the *same*
    customer are not. A per-case check would happily send one person four
    messages in an afternoon, one per failed invoice, each individually
    defensible. The ledger below is what stops that.
    """

    def __init__(self, config: PolicyConfig | None = None) -> None:
        self.config = config or PolicyConfig()
        self._contacts_this_run: dict[str, int] = defaultdict(int)
        self._last_contact_at: dict[str, datetime] = {}
        self._contacts_per_case: dict[str, int] = defaultdict(int)
        self._retries_per_case: dict[str, int] = defaultdict(int)
        #: Rule -> how many times it fired. Reported at the end of a run so the
        #: guardrails are visible as numbers rather than as prose in a README.
        self.rule_fires: dict[str, int] = defaultdict(int)

    # -- helpers ---------------------------------------------------------

    def _local_hour(self, when: datetime, customer: Customer) -> int:
        return (when + timedelta(minutes=customer.timezone_offset_min)).hour

    def _next_allowed_hour(self, when: datetime, customer: Customer) -> datetime:
        """Push a timestamp forward to the next moment outside quiet hours."""
        local = when + timedelta(minutes=customer.timezone_offset_min)
        cfg = self.config
        if local.hour >= cfg.quiet_hours_start:
            local = (local + timedelta(days=1)).replace(
                hour=cfg.quiet_hours_end, minute=0, second=0, microsecond=0
            )
        elif local.hour < cfg.quiet_hours_end:
            local = local.replace(
                hour=cfg.quiet_hours_end, minute=0, second=0, microsecond=0
            )
        return local - timedelta(minutes=customer.timezone_offset_min)

    def _pick_channel(self, customer: Customer) -> Channel | None:
        for ch in CHANNEL_PREFERENCE:
            if ch in customer.consented_channels:
                return ch
        return None

    def _fire(self, rule: str) -> str:
        self.rule_fires[rule] += 1
        return rule

    # -- the gate --------------------------------------------------------

    def evaluate(
        self,
        *,
        signal: Signal,
        customer: Customer,
        diagnosis: Diagnosis,
        proposed: Intervention,
        now: datetime,
    ) -> PolicyVerdict:
        """Decide whether ``proposed`` may happen, and in what form."""
        cfg = self.config
        blocked: list[str] = []
        notes: list[str] = []

        # -- Rules that apply to every intervention -----------------------

        # An open dispute means this money is contested. Chasing it while the
        # customer is arguing with their bank is how a complaint becomes a
        # regulatory one. Only a human may touch it.
        if customer.dispute_open and proposed is not Intervention.ESCALATE_HUMAN:
            blocked.append(self._fire("dispute_open"))
            notes.append(
                "customer has an open dispute; automated recovery is suspended "
                "and the case is routed to a human"
            )
            return PolicyVerdict(
                allowed=False,
                blocked_by=tuple(blocked),
                notes=tuple(notes),
                substituted=Intervention.ESCALATE_HUMAN,
            )

        # Chasing amounts smaller than the cost of chasing them is negative-sum.
        if (
            signal.amount_paise < cfg.min_amount_to_pursue_paise
            and proposed is not Intervention.STOP
        ):
            blocked.append(self._fire("below_pursue_floor"))
            notes.append(
                f"amount is under the Rs {cfg.min_amount_to_pursue_paise / 100:.0f} "
                "floor; pursuit costs more than the money is worth"
            )
            return PolicyVerdict(
                allowed=False,
                blocked_by=tuple(blocked),
                notes=tuple(notes),
                substituted=Intervention.STOP,
            )

        # Acting confidently on a guess is how automated systems do damage at
        # scale. Under the floor, a human looks at it.
        if (
            diagnosis.confidence < cfg.min_confidence_to_act
            and proposed not in (Intervention.STOP, Intervention.ESCALATE_HUMAN)
        ):
            blocked.append(self._fire("low_confidence"))
            notes.append(
                f"diagnosis confidence {diagnosis.confidence:.2f} is below the "
                f"{cfg.min_confidence_to_act:.2f} floor; routing to a human "
                "rather than acting on a guess"
            )
            return PolicyVerdict(
                allowed=False,
                blocked_by=tuple(blocked),
                notes=tuple(notes),
                substituted=Intervention.ESCALATE_HUMAN,
            )

        # -- Retry rules ---------------------------------------------------

        if proposed in (Intervention.RETRY_NOW, Intervention.RETRY_SCHEDULED):
            if diagnosis.root_cause in cfg.no_retry_causes:
                blocked.append(self._fire("futile_retry"))
                notes.append(
                    f"retrying a {diagnosis.root_cause} will not succeed and "
                    "inflates the merchant's decline rate with the issuer"
                )
                substitute = (
                    Intervention.REQUEST_NEW_INSTRUMENT
                    if diagnosis.root_cause is RootCause.STALE_INSTRUMENT
                    else Intervention.STOP
                )
                return PolicyVerdict(
                    allowed=False,
                    blocked_by=tuple(blocked),
                    notes=tuple(notes),
                    substituted=substitute,
                )

            if self._retries_per_case[signal.case_id] >= cfg.max_retries_per_case:
                blocked.append(self._fire("retry_cap"))
                notes.append(
                    f"case already retried {cfg.max_retries_per_case} times"
                )
                return PolicyVerdict(
                    allowed=False,
                    blocked_by=tuple(blocked),
                    notes=tuple(notes),
                    substituted=Intervention.STOP,
                )

            self._retries_per_case[signal.case_id] += 1
            return PolicyVerdict(allowed=True, notes=("retry permitted",))

        # -- Contact rules -------------------------------------------------

        if proposed in CONTACT_INTERVENTIONS:
            # Opt-out is absolute. It is not weighed against the amount, the
            # confidence, or how likely the message is to work.
            if customer.opted_out:
                blocked.append(self._fire("opted_out"))
                notes.append("customer has opted out of contact; this is absolute")
                return PolicyVerdict(
                    allowed=False,
                    blocked_by=tuple(blocked),
                    notes=tuple(notes),
                    substituted=Intervention.STOP,
                )

            # A promise to pay is a commitment we asked for. Messaging someone
            # before the date they gave us teaches them the promise was
            # pointless.
            ptp = diagnosis.extracted_promise_date or customer.promise_to_pay_date
            if ptp is not None:
                hold_until = ptp + timedelta(days=cfg.promise_to_pay_grace_days)
                if now < hold_until:
                    blocked.append(self._fire("promise_to_pay_hold"))
                    notes.append(
                        f"customer committed to pay by {ptp.date().isoformat()}; "
                        f"no contact until {hold_until.date().isoformat()}"
                    )
                    return PolicyVerdict(
                        allowed=False,
                        blocked_by=tuple(blocked),
                        notes=tuple(notes),
                        substituted=Intervention.STOP,
                        scheduled_for=hold_until,
                    )

            if self._contacts_per_case[signal.case_id] >= cfg.max_contacts_per_case:
                blocked.append(self._fire("contact_cap_case"))
                notes.append(
                    f"already contacted {cfg.max_contacts_per_case} times about "
                    "this case"
                )
                return PolicyVerdict(
                    allowed=False,
                    blocked_by=tuple(blocked),
                    notes=tuple(notes),
                    substituted=Intervention.STOP,
                )

            used = (
                customer.contacts_this_week
                + self._contacts_this_run[customer.customer_id]
            )
            if used >= cfg.max_contacts_per_customer_per_week:
                blocked.append(self._fire("contact_cap_customer"))
                notes.append(
                    f"customer already received {used} contacts this week across "
                    "all cases; the weekly cap is per person, not per invoice"
                )
                return PolicyVerdict(
                    allowed=False,
                    blocked_by=tuple(blocked),
                    notes=tuple(notes),
                    substituted=Intervention.STOP,
                )

            last = self._last_contact_at.get(customer.customer_id)
            if last is not None and (now - last) < timedelta(
                hours=cfg.min_hours_between_contacts
            ):
                blocked.append(self._fire("cooloff"))
                notes.append(
                    f"last contact was under {cfg.min_hours_between_contacts}h ago"
                )
                return PolicyVerdict(
                    allowed=False,
                    blocked_by=tuple(blocked),
                    notes=tuple(notes),
                    substituted=Intervention.STOP,
                )

            channel = self._pick_channel(customer)
            if channel is None:
                blocked.append(self._fire("no_consented_channel"))
                notes.append("no channel this customer has consented to")
                return PolicyVerdict(
                    allowed=False,
                    blocked_by=tuple(blocked),
                    notes=tuple(notes),
                    substituted=Intervention.STOP,
                )

            # Quiet hours are a delay, not a veto. This is the substitution the
            # module docstring describes: the right answer is "later", and a
            # system that only knows "yes" and "no" would throw the recovery
            # away to avoid the 11pm SMS.
            scheduled_for: datetime | None = None
            hour = self._local_hour(now, customer)
            if hour >= cfg.quiet_hours_start or hour < cfg.quiet_hours_end:
                self._fire("quiet_hours")
                scheduled_for = self._next_allowed_hour(now, customer)
                notes.append(
                    f"local time is {hour:02d}:00; deferred to "
                    f"{scheduled_for.isoformat()} rather than cancelled"
                )

            self._contacts_this_run[customer.customer_id] += 1
            self._contacts_per_case[signal.case_id] += 1
            self._last_contact_at[customer.customer_id] = now
            notes.append(f"contact permitted on {channel}")
            return PolicyVerdict(
                allowed=True,
                notes=tuple(notes),
                scheduled_for=scheduled_for,
                channel=channel,
            )

        # ESCALATE_HUMAN and STOP need no gating -- neither one reaches the
        # customer or the issuer.
        return PolicyVerdict(allowed=True, notes=("no gate applies",))

    def snapshot(self) -> dict[str, int]:
        """Rule fire counts, for the run report."""
        return dict(sorted(self.rule_fires.items(), key=lambda kv: -kv[1]))
