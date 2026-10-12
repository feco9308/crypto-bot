"""Fixed-hypothesis replay research exits and snapshot-based setup re-arm."""

from crypto_bot.replay.clock import at
from crypto_bot.replay.institutional_exit import ParameterizedPeakLock
from crypto_bot.replay.shadow_adaptive import PeakState
from crypto_bot.trading.domain import D

BE = "break_even_guard_v1"
DELAYED = "delayed_peak_lock_v2"
DEFAULTS = {
    BE: {"activation_break_even_r": "1.0"},
    DELAYED: {
        "initial_stop_atr": "2.0",
        "break_even_activation_r": "1.0",
        "peak_trailing_activation_r": "2.0",
        "peak_trail_atr_multiplier": "1.25",
        "tightening_activation_r": "4.0",
        "tight_trail_atr_multiplier": "0.75",
    },
}
STAGES = ("ORIGINAL_STOP", "BREAK_EVEN", "PEAK_TRAIL", "TIGHT_PEAK_TRAIL")


class ExitV2(ParameterizedPeakLock):
    def __init__(self, entry, settings, name, parameters):
        self.name = name
        p = DEFAULTS[name] | parameters
        activation = D(
            p.get("activation_break_even_r", p.get("break_even_activation_r"))
        )
        mapped = dict(
            activation_break_even_r=activation,
            activation_trailing_r=p.get("peak_trailing_activation_r", activation + 1),
            tighten_trailing_r=p.get("tightening_activation_r", activation + 3),
            trail_atr_multiplier=p.get("peak_trail_atr_multiplier", "1.25"),
            tight_trail_atr_multiplier=p.get("tight_trail_atr_multiplier", "0.75"),
        )
        super().__init__(entry, settings, mapped)

    def ratchet(self, state, price, now):
        if self.name != BE:
            return super().ratchet(state, price, now)
        highest = max(state.highest, price)
        stage = int(highest >= self.thresholds[0])
        timestamp = state.times[0] or (at(now).isoformat() + "Z" if stage else None)
        return PeakState(
            max(state.stop, self.break_even) if stage else state.stop,
            highest,
            stage,
            (timestamp, None, None),
        )

    def audit(self, result=None):
        out = super().audit(result)
        for key in ("activation_075r_time", "activation_1r_time", "activation_2r_time"):
            out.pop(key)
        out.update(
            activation_break_even_time=self.state.times[0],
            activation_1r_time=self.state.times[0],
            activation_2r_time=self.state.times[1],
            activation_4r_time=self.state.times[2],
            peak_trailing_activation_time=self.state.times[1],
            tightening_activation_time=self.state.times[2],
            exit_stage=STAGES[self.state.stage],
        )
        return out


class SetupRearm:
    """Only snapshots strictly later than a protective exit may reset a setup.

    ARMED_AFTER_FALSE also needs a later snapshot for TRUE; execution cannot
    reuse a pre-exit signal. Portfolio constraints are excluded by the caller.
    """

    def __init__(self, state=None):
        self.symbols = (state or {}).get("symbols", {})
        self.blocks = (state or {}).get("blocks", 0)
        self.successes = (state or {}).get("successes", 0)
        self.durations = (state or {}).get("durations", [])

    def closed(self, symbol, stage, now):
        if stage in STAGES[1:]:
            self.symbols[symbol] = dict(exit_time=now, false_time=None, armed=False)

    def observe(self, symbol, setup, now):
        state = self.symbols.get(symbol)
        if not state or now <= state["exit_time"]:
            return False
        if not setup and state["false_time"] is None:
            state["false_time"] = now
            self.successes += 1
            self.durations.append((now - state["exit_time"]) / 1000)
            return True
        if setup and state["false_time"] is not None and now > state["false_time"]:
            state["armed"] = True
        return False

    def allowed(self, symbol, signal_time):
        state = self.symbols.get(symbol)
        return not state or (state["armed"] and signal_time > state["false_time"])

    def checkpoint(self):
        return dict(
            symbols=self.symbols,
            blocks=self.blocks,
            successes=self.successes,
            durations=self.durations,
        )

    def metrics(self):
        return dict(
            rearm_block_count=self.blocks,
            rearm_success_count=self.successes,
            average_time_to_rearm=sum(self.durations) / len(self.durations)
            if self.durations
            else None,
        )


def evaluation(configs, results):
    """Reporting only: prespecified diagnostics, no winner or trade feedback."""
    baseline = next(
        (
            i
            for i, v in enumerate(configs)
            if v["strategy"] == "institutional_style_v1"
            and v["exit_policy"] == "baseline_v1"
        ),
        None,
    )
    if baseline is None:
        return None
    control = results[baseline]["metrics"]
    warnings = []
    for i, result in enumerate(results):
        m = result["metrics"]
        if i == baseline:
            continue
        flags = []
        if m["trade_count"] >= 1.5 * control["trade_count"] and control["trade_count"]:
            flags.append("TRADE_COUNT_AT_LEAST_50PCT_HIGHER_THAN_CONTROL")
        if (
            m.get("largest_realized_R") is not None
            and control.get("largest_realized_R") is not None
            and m["largest_realized_R"] < control["largest_realized_R"]
        ):
            flags.append("LARGE_WINNER_PROXY_REDUCED_UNMATCHED_PORTFOLIO")
        if (
            m.get("profit_factor") is not None
            and control.get("profit_factor") is not None
            and m.get("expectancy_r") is not None
            and control.get("expectancy_r") is not None
            and m["profit_factor"] > control["profit_factor"]
            and m["win_rate"] > control["win_rate"]
            and m["expectancy_r"] <= control["expectancy_r"]
        ):
            flags.append("HIGHER_PF_AND_WIN_RATE_WITHOUT_EXPECTANCY_R_IMPROVEMENT")
        if (
            D(m["turnover_notional"]) > D(control["turnover_notional"])
            and D(m["fees"]) > D(control["fees"])
            and m["return_pct"] < control["return_pct"]
        ):
            flags.append("HIGHER_TURNOVER_FEES_WITH_WORSE_NET_RETURN")
        warnings.append(dict(variant=i, control=baseline, warnings=flags))
    return dict(
        primary_metrics=["expectancy_r", "profit_factor", "return_pct", "max_drawdown"],
        winner_selected=False,
        warnings=warnings,
        diagnostic_note="50% trade-count flag is descriptive, not statistical significance. Largest R is an unmatched-portfolio proxy, not causal matched-entry evidence.",
    )
