"""Replay-only admission. Observations never touch strategy or portfolio state."""

from copy import deepcopy
from dataclasses import replace

from crypto_bot.replay.clock import at

NAME = "fresh_setup_admission_v1"
HOUR = 3600000


def setup_observation(strategy, context):
    """Reuse the complete entry predicate, excluding holdings/correlation only.

    Required missing momentum or timeframe/BTC context is unobservable, not FALSE.
    A missing symbol has no observation at all. No archive coverage is inspected.
    """
    audit = context.features.get("institutional", {})
    neutral = replace(
        context,
        features=context.features
        | {"institutional": audit | {"correlation": {"blocked": False, "pairs": []}}},
    )
    signal = strategy.evaluate(neutral, False)
    observable = not any("UNAVAILABLE" in r for r in signal.reasons)
    if strategy.settings.require_delta and context.delta is None:
        observable = False
    return signal.action == "BUY", observable


class FreshSetupAdmission:
    def __init__(self, checkpoint=None):
        self.states = deepcopy(checkpoint or {})

    def observe(self, symbol, setup, observable, now):
        state = self.states.setdefault(
            symbol,
            dict(
                current=None,
                previous=None,
                true_since=None,
                fresh_time=None,
                observed_at=None,
                attempted=False,
                filled=False,
                missed=False,
                armed_count=0,
            ),
        )
        # Re-reading a snapshot must not generate another transition/admission.
        if not observable or state["observed_at"] == now:
            return None
        previous = state["current"]
        state.update(previous=previous, current=setup, observed_at=now)
        if not setup:
            state.update(
                true_since=None,
                fresh_time=None,
                attempted=False,
                filled=False,
                missed=False,
            )
            return None
        if previous is not True:
            state.update(
                true_since=now,
                fresh_time=None,
                attempted=False,
                filled=False,
                missed=False,
            )
        if previous is False:
            state["fresh_time"] = now
            state["armed_count"] += 1
            return (
                "FRESH_SETUP_REARMED"
                if state["armed_count"] > 1
                else "FRESH_SETUP_ARMED"
            )
        return None

    def admit(self, symbol, now):
        state = self.states[symbol]
        if state["fresh_time"] != now or state["attempted"]:
            return False
        state["attempted"] = True
        return True

    def missed(self, symbol):
        state = self.states.get(symbol)
        if not state or not state["attempted"] or state["filled"] or state["missed"]:
            return False
        state["missed"] = True
        return True

    def filled(self, symbol):
        self.states[symbol]["filled"] = True

    def audit(self, symbol, now):
        s = self.states[symbol]
        return dict(
            previous_setup_state=s["previous"],
            current_setup_state=s["current"],
            setup_true_since=at(s["true_since"]).isoformat() + "Z"
            if s["true_since"] is not None
            else None,
            setup_age_hours=(now - s["true_since"]) / HOUR
            if s["true_since"] is not None
            else None,
        )

    def checkpoint(self):
        return deepcopy(self.states)
