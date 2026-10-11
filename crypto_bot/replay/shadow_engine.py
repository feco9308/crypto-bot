"""Exit-only replay of frozen actual BUYs. No wallet, risk manager or order sender."""

import copy
import time
from types import SimpleNamespace

from crypto_bot.config.settings import Settings
from crypto_bot.indicators.technical import features
from crypto_bot.replay import stale
from crypto_bot.replay.clock import at, ms
from crypto_bot.replay.config import config_hash, settings_for
from crypto_bot.replay.exits import levels, observe, policy
from crypto_bot.replay.registry import SCORES, STRATEGIES
from crypto_bot.replay.shadow_metrics import analysis
from crypto_bot.replay.universe import CandleHistory, HistoricalUniverse
from crypto_bot.trading.domain import D, MarketContext, q

MINUTE = 60000
HOUR = 3600000
DAY = 24 * HOUR


class ShadowPosition:
    def __init__(self, entry, definition, settings, stale_policy):
        self.entry = copy.deepcopy(entry)
        self.definition = definition
        self.settings = settings
        self.stale_policy = stale_policy
        self.events = []
        self.data_complete = True
        self.strategy_data_missing = False
        self.result = None
        price = D(entry["entry_fill_price"])
        stop = D(entry["original_stop_price"])
        self.model = policy(definition["exit_policy"], definition["exit_parameters"])
        tp, _ = levels(self.model, price, price - stop)
        self.p = SimpleNamespace(
            id=entry["shadow_entry_id"],
            symbol=entry["symbol"],
            status="OPEN",
            entry_price=price,
            current_price=price,
            quantity=D(entry["quantity"]),
            notional=D(entry["notional"]),
            entry_fee=D(entry["entry_fee"]),
            original_stop=stop,
            stop_price=stop,
            highest=price,
            overlay_active=False,
            take_profit_price=tp,
            entry_data={},
        )
        self.mfe, self.mae, self.ambiguous = price, price, False
        value = stale.state(self.p)
        value["last_valid_mark_time"] = entry["entry_time"]
        value["last_valid_mark_price"] = entry["entry_fill_price"]

    def event(self, kind, now, **values):
        self.events.append(
            dict(event=kind, timestamp=at(now).isoformat() + "Z", **values)
        )

    def fresh(self, fresh, now):
        if self.result:
            return
        stale.update(self.p, fresh, now, self.event, self.stale_policy)
        if not fresh:
            self.data_complete = False

    def mark(self, price, now):
        self.p.current_price = D(price)
        value = stale.state(self.p)
        value["last_valid_mark_time"] = at(now).isoformat() + "Z"
        value["last_valid_mark_price"] = str(D(price))

    def close(self, reference, now, reason, precision, reasons=()):
        if now <= ms(self.entry["entry_time"]):
            raise ValueError("Shadow exit must be strictly after canonical entry")
        if stale.state(self.p)["stale"]:
            raise ValueError("No shadow fill without current execution data")
        price = q(D(reference) * (1 - self.settings.paper_slippage_pct / D(100)))
        notional = q(price * self.p.quantity)
        fee = q(notional * self.settings.paper_fee_pct / D(100))
        pnl = q(notional - fee - self.p.notional - self.p.entry_fee)
        cost = self.p.notional + self.p.entry_fee
        risk = D(self.entry["risk_amount"])
        slip = q(abs(price - D(reference)) * self.p.quantity)
        self.p.status = "CLOSED"
        self.result = dict(
            exit_time=at(now).isoformat() + "Z",
            exit_price=str(price),
            exit_reference_price=str(D(reference)),
            exit_reason=reason,
            strategy_reasons=list(reasons),
            exit_fee=str(fee),
            fees=str(self.p.entry_fee + fee),
            exit_slippage=str(slip),
            slippage=str(D(self.entry["entry_slippage"]) + slip),
            realized_pnl=str(pnl),
            return_pct=float(pnl / cost * 100),
            normalized_pnl_per_1000=float(pnl / cost * 1000),
            r_multiple=float(pnl / risk) if risk > 0 else None,
            duration_seconds=(now - ms(self.entry["entry_time"])) / 1000,
            execution_precision=precision,
        )
        self.event(
            "SHADOW_EXIT_FILL",
            now,
            reference=str(reference),
            price=str(price),
            reason=reason,
            fee=str(fee),
            slippage=str(slip),
        )

    def bar(self, candle, now, conservative):
        if self.result:
            return
        if candle["close_time"] >= now or candle["time"] < ms(self.entry["entry_time"]):
            raise ValueError("Shadow requires a completed post-entry candle")
        self.fresh(True, now)
        self.mark(candle["close"], now)
        hit, ambiguous, visited = observe(candle, self.p, self.model, conservative)
        self.ambiguous |= ambiguous
        self.mfe = max(self.mfe, *visited)
        self.mae = min(self.mae, *visited)
        if hit:
            self.close(hit[0], now, hit[1], "INTRABAR_APPROXIMATION_KNOWN_AT_CLOSE")

    def export(self, now):
        value = stale.state(self.p)
        status = (
            "INCOMPLETE_DATA"
            if self.result and not self.data_complete
            else "CLOSED"
            if self.result
            else "OPEN_STALE"
            if value["stale"]
            else "OPEN"
        )
        return dict(
            shadow_entry_id=self.entry["shadow_entry_id"],
            policy_id=self.definition["id"],
            policy=self.definition["exit_policy"],
            policy_parameters=self.definition["exit_parameters"],
            symbol=self.entry["symbol"],
            status=status,
            shadow_status="SHADOW_OPEN_STALE" if status == "OPEN_STALE" else status,
            position_status=self.p.status,
            data_complete=self.data_complete,
            comparison_eligible=status == "CLOSED" and self.data_complete,
            entry_time=self.entry["entry_time"],
            entry_fill_price=self.entry["entry_fill_price"],
            original_stop=self.entry["original_stop_price"],
            quantity=self.entry["quantity"],
            canonical_notional=self.entry["notional"],
            canonical_risk_amount=self.entry["risk_amount"],
            entry_atr=self.entry["atr"],
            entry_fee=self.entry["entry_fee"],
            entry_slippage=self.entry["entry_slippage"],
            exit_time=None,
            exit_price=None,
            exit_reason=None,
            return_pct=None,
            realized_pnl=None,
            normalized_pnl_per_1000=None,
            r_multiple=None,
            duration_seconds=(now - ms(self.entry["entry_time"])) / 1000,
            mfe_pct=max(0, float((self.mfe / self.p.entry_price - 1) * 100)),
            mae_pct=min(0, float((self.mae / self.p.entry_price - 1) * 100)),
            intrabar_ambiguity=self.ambiguous,
            first_stale_time=value["first_stale_time"],
            last_valid_mark_time=value["last_valid_mark_time"],
            last_valid_mark_price=value["last_valid_mark_price"],
            stale_duration_seconds=stale.duration(value, now),
            resumed_after_stale=value["resumed_after_stale"],
            events=self.events,
        ) | (self.result or {})


class ShadowEngine:
    def __init__(self, provider, check=None, progress=None):
        self.provider = provider
        self.check = check or (lambda: None)
        self.progress = progress or (lambda **_: None)
        self.hourly_signals = {}

    def strategy_signal(self, symbol, now, universe, scanner, strategy, score_version):
        key = symbol, now
        if key not in self.hourly_signals:
            market = universe.market(symbol, now)
            if market is None:
                self.hourly_signals[key] = None
            else:
                observed = [c for c, _ in universe.as_of(symbol, now)]
                assert all(c.close_time < now for c in observed)
                f = features(observed, market, scanner)
                score = SCORES.get(score_version)(f, scanner)["total_score"]
                ctx = MarketContext(
                    now // HOUR,
                    symbol,
                    at(now),
                    D(f["price"]),
                    D(score),
                    None,
                    D(f["rsi"]),
                    D(f["ema50"]),
                    D(f["ema200"]),
                    D(f["atr"]),
                    D(f["relative_volume"]),
                    "WATCH",
                    True,
                    f,
                )
                # Exit branch only. No shadow BUY signal can create an entry.
                self.hourly_signals[key] = strategy.evaluate(ctx, has_position=True)
        return self.hourly_signals[key]

    def datasets(self, symbol, start, end, fallback):
        result = self.provider.minutes(symbol, start, end)
        if fallback:
            other = self.provider.minutes(symbol, start, end, "5m")
            for t, c in other.items():
                if not any(m in result for m in range(t, t + 300000, MINUTE)):
                    result[t] = c
        return result

    def run(self, job):
        began = time.monotonic()
        source, config = job["source"], job["config"]
        anchor = source["config"]
        start, end = ms(anchor["start"]), ms(anchor["end"])
        settings = settings_for(source["variant"])
        scanner = Settings(**anchor["scanner_settings"])
        strategy = STRATEGIES.get(source["variant"]["strategy"]).factory(settings)
        entries, results = source["entries"], []
        histories = {}
        # Entire archive may be cached, but evaluation below only uses as_of(now).
        for symbol in sorted({e["symbol"] for e in entries}):
            self.check()
            histories[symbol] = CandleHistory(
                self.provider.hourly(symbol, start - anchor["warmup_hours"] * HOUR, end)
            )
        universe = HistoricalUniverse(
            histories, scanner, float(anchor["spread_approximation_pct"])
        )
        for i, entry in enumerate(entries):
            self.check()
            positions = [
                ShadowPosition(
                    entry,
                    p,
                    settings,
                    anchor.get("stale_position_policy", stale.STRICT),
                )
                for p in config["policies"]
            ]
            cursor = ms(entry["entry_time"])
            while cursor < end and any(not p.result for p in positions):
                self.check()
                stop = min(end, (cursor // DAY + 1) * DAY)
                data = self.datasets(
                    entry["symbol"], cursor, stop, anchor["fallback_5m"]
                )
                for minute in range(cursor, stop, MINUTE):
                    self.check()
                    active = [p for p in positions if not p.result]
                    if not active:
                        break
                    current = data.get(minute)
                    if minute % HOUR == 0:
                        for p in active:
                            p.fresh(current is not None, minute)
                            if current:
                                p.mark(current["open"], minute)
                        if current and minute > ms(entry["entry_time"]):
                            signal = self.strategy_signal(
                                entry["symbol"],
                                minute,
                                universe,
                                scanner,
                                strategy,
                                anchor["score_version"],
                            )
                            if signal is None:
                                for p in active:
                                    p.data_complete = False
                                    if not p.strategy_data_missing:
                                        p.event(
                                            "MISSING_CLOSED_1H_STRATEGY_DATA", minute
                                        )
                                        p.strategy_data_missing = True
                            if signal is not None and signal.action == "SELL":
                                for p in active:
                                    p.close(
                                        current["open"],
                                        minute,
                                        "STRATEGY",
                                        "CANDLE_BOUNDARY_APPROXIMATION",
                                        signal.reasons,
                                    )
                    now = minute + MINUTE
                    fallback_bar = data.get(minute - 240000)
                    if fallback_bar and fallback_bar["interval"] != "5m":
                        fallback_bar = None
                    bar = (
                        current
                        if current and current["interval"] == "1m"
                        else fallback_bar
                    )
                    if bar and bar["interval"] not in ("1m", "5m"):
                        raise ValueError("Unsupported shadow execution interval")
                    # A pending 5m candle is not a missing intervening minute.
                    pending = data.get(minute - minute % 300000)
                    fresh = bool(bar or (pending and pending["interval"] == "5m"))
                    for p in active:
                        if not p.result:
                            p.fresh(fresh, now)
                            if bar:
                                p.bar(bar, now, anchor["conservative"])
                cursor = stop
            results.extend(
                p.export(end if not p.result else ms(p.result["exit_time"]))
                for p in positions
            )
            self.progress(
                canonical_entries_processed=i + 1, canonical_entry_count=len(entries)
            )
        revisions = (
            self.provider.cache.revision(self.provider.used)
            if hasattr(self.provider, "cache")
            else {}
        )
        prior = source["metadata"].get("historical_data_revision", {})
        changed = [k for k, v in revisions.items() if k in prior and v != prior[k]]
        if changed:
            raise ValueError(
                "Archive revisions differ from anchor; cannot compare reproducibly"
            )
        metadata = job["metadata"] | dict(
            anchor_run_id=config["anchor_run_id"],
            anchor_variant=config["anchor_variant"],
            dataset_role=anchor["dataset_role"],
            start=anchor["start"],
            end=anchor["end"],
            stale_position_policy=anchor.get("stale_position_policy", stale.STRICT),
            conservative=anchor["conservative"],
            fallback_5m=anchor["fallback_5m"],
            strict_no_lookahead=True,
            entry_source="PERSISTED_ANCHOR_FILLED_BUY",
            strategy_exit_model="REGISTERED_STRATEGY_CLOSED_1H_EXIT_BRANCH",
            execution_model="BINANCE_PUBLIC_KLINES_APPROXIMATION",
            historical_archive_revisions=revisions,
            historical_archive_hash=config_hash(revisions),
            anchor_archive_manifest_hash=config_hash(prior),
            elapsed_seconds=time.monotonic() - began,
            warning="MATCHED-ENTRY EXIT COMPARISON — NOT PORTFOLIO PERFORMANCE. NOT STATISTICALLY VALIDATED.",
            incomplete_rule="Any observed price gap excludes that policy outcome from paired wins/losses, even after resume.",
            return_denominator="CANONICAL_NOTIONAL_PLUS_ENTRY_FEE",
            normalization="RETURN_PERCENT; R_PER_CANONICAL_RISK; PNL_PER_1000_ENTRY_COST",
            outcomes_hash=config_hash(
                sorted(results, key=lambda r: (r["shadow_entry_id"], r["policy_id"]))
            ),
        )
        return dict(
            metadata=metadata,
            anchor_config=anchor,
            anchor_variant_config=source["variant"],
            shadow_policies=config["policies"],
            canonical_entry_count=len(entries),
            **analysis(entries, results),
            canonical_entries=entries,
            outcomes=results,
        )
