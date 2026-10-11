"""Fixed research-only peak lock; never registered with normal replay exits."""

from dataclasses import dataclass
from decimal import ROUND_CEILING

from crypto_bot.replay.clock import at
from crypto_bot.trading.domain import D, q

NAME = "adaptive_peak_lock_v1"


@dataclass(frozen=True)
class PeakState:
    stop: object
    highest: object
    stage: int = 0
    times: tuple = (None, None, None)


class AdaptivePeakLock:
    def __init__(self, entry, settings):
        self.entry = D(entry["entry_fill_price"])
        self.original = D(entry["original_stop_price"])
        self.quantity = D(entry["quantity"])
        self.risk = D(entry["risk_amount"])
        self.atr = D(str(entry["atr"]))
        if min(self.quantity, self.risk, self.atr) <= 0:
            raise ValueError(
                "Adaptive peak lock requires positive quantity, risk and entry ATR"
            )
        self.fee = settings.paper_fee_pct / D(100)
        self.slip = settings.paper_slippage_pct / D(100)
        self.cost = D(entry["notional"]) + D(entry["entry_fee"])
        # Entry fill/notional already includes entry slippage. Do not charge it twice.
        unit = D("0.000000000001")
        # Account for quantization in BOTH the sell fill and the cash/fee ledger.
        required_fill = ((self.cost + unit) / self.quantity / (1 - self.fee)).quantize(
            unit, rounding=ROUND_CEILING
        )
        self.break_even = (required_fill / (1 - self.slip)).quantize(
            unit, rounding=ROUND_CEILING
        )
        self.thresholds = tuple(
            self.entry + self.risk / self.quantity * r for r in (D("0.75"), D(1), D(2))
        )
        self.state = PeakState(self.original, self.entry)

    def ratchet(self, state, price, now):
        highest = max(state.highest, price)
        stage = sum(highest >= x for x in self.thresholds)
        times = tuple(
            old or (at(now).isoformat() + "Z" if stage > i else None)
            for i, old in enumerate(state.times)
        )
        stop = state.stop
        if stage:
            stop = max(stop, self.break_even)
        if stage >= 2:
            stop = max(
                stop, highest - self.atr * (D("0.50") if stage == 3 else D("0.75"))
            )
        return PeakState(stop, highest, stage, times)

    def walk(self, points, state, now):
        visited = [points[0]]
        if points[0] <= state.stop:
            return state, points[0], visited
        state = self.ratchet(state, points[0], now)
        if points[0] <= state.stop:
            return state, points[0], visited
        for previous, price in zip(points, points[1:]):
            if price >= previous:
                state = self.ratchet(state, price, now)
                if price <= state.stop:
                    visited.append(price)
                    return state, price, visited
            elif price <= state.stop:
                visited.append(state.stop)
                return state, state.stop, visited
            visited.append(price)
        return state, None, visited

    def observe(self, candle, position, now, conservative):
        o, h, lo, c = [D(str(candle[k])) for k in ("open", "high", "low", "close")]
        paths = {(o, h, lo, c), (o, lo, h, c)}
        # Same local OHLC ambiguity model as existing overlays: include earliest
        # activation/partial-peak retracement before the full candle high.
        for threshold in self.thresholds:
            peak = max(o, self.state.highest, threshold)
            if o <= peak <= h:
                paths.add((o, peak, lo, h, c))
        for threshold, distance in (
            (self.thresholds[1], D("0.75")),
            (self.thresholds[2], D("0.50")),
        ):
            peak = max(o, self.state.highest, threshold, lo + self.atr * distance)
            if o <= peak <= h:
                paths.add((o, peak, lo, h, c))
        candidates, survivors = [], []
        for points in sorted(paths):
            state, hit, visited = self.walk(points, self.state, now)
            (survivors if hit is None else candidates).append(
                (hit, points, state, visited)
            )
        if candidates:
            candidates.sort(key=lambda x: (x[0], x[1]))
            hit, _, state, visited = candidates[0] if conservative else candidates[-1]
            ambiguous = len({x[0] for x in candidates}) > 1 or bool(survivors)
            reason = "ORIGINAL_STOP" if state.stage == 0 else "ADAPTIVE_PEAK_LOCK"
            result = (hit, reason)
        else:
            _, _, state, visited = survivors[0]
            ambiguous, result = False, None
        self.state = state
        position.highest, position.stop_price = state.highest, state.stop
        position.overlay_active = state.stage > 0
        return result, ambiguous, visited

    def audit(self, result):
        s = self.state
        fill = q(s.highest * (1 - self.slip))
        notional = q(fill * self.quantity)
        peak_pnl = notional - q(notional * self.fee) - self.cost
        pnl = D(result["realized_pnl"]) if result else None
        return dict(
            activation_075r_time=s.times[0],
            activation_1r_time=s.times[1],
            activation_2r_time=s.times[2],
            highest_price_since_entry=str(s.highest),
            highest_profit_pct=float(peak_pnl / self.cost * 100),
            highest_r=float((s.highest - self.entry) * self.quantity / self.risk),
            highest_net_r=float(peak_pnl / self.risk),
            break_even_stop=str(self.break_even),
            final_protective_stop=str(s.stop),
            exit_stage=(
                "ORIGINAL_STOP",
                "BREAK_EVEN_075R",
                "PEAK_TRAIL_1R",
                "TIGHT_PEAK_TRAIL_2R",
            )[s.stage],
            realized_return_pct=result["return_pct"] if result else None,
            realized_r=result["r_multiple"] if result else None,
            giveback_pct=float((peak_pnl - pnl) / self.cost * 100)
            if pnl is not None
            else None,
            giveback_r=float((peak_pnl - pnl) / self.risk) if pnl is not None else None,
        )
