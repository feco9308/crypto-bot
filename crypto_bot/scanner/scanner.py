from datetime import timedelta
import logging
from crypto_bot.indicators.technical import features
from crypto_bot.scanner.scoring import score
from crypto_bot.scanner.momentum import score_momentum
from crypto_bot.scanner.selection import eligible, select_watchlist, universe
from crypto_bot.storage.database import utcnow

log = logging.getLogger(__name__)


class Scanner:
    def __init__(self, provider, database, settings):
        self.provider, self.database, self.settings = provider, database, settings

    def run_once(self, timestamp=None):
        timestamp = timestamp or utcnow()
        settings = self.settings
        run_id = self.database.start_run(settings, timestamp)
        log.info("SCANNER_START", extra={"event_data": {"run_id": run_id}})
        previous = {r["symbol"] for r in self.database.latest_rows(settings.timeframe) if r["algorithm_watch"]}
        try:
            markets = self.provider.markets()
            candidates = universe(markets, settings)
            overrides = self.database.overrides()
            manual = [m for m in markets if overrides.get(m.symbol) in {"WATCH", "PINNED"} and eligible(m, settings, manual=True)]
            all_candidates = {m.symbol: m for m in candidates + manual}
            rows, errors = [], {}
            for symbol, status in overrides.items():
                if status in {"WATCH", "PINNED"} and symbol not in all_candidates:
                    errors[symbol] = "Manual instrument unavailable or inactive"
            for market in all_candidates.values():
                try:
                    candles = self.provider.candles(market.symbol, settings.timeframe, settings.candle_limit)
                    now_ms = int(timestamp.replace(tzinfo=__import__('datetime').timezone.utc).timestamp() * 1000)
                    closed = [c for c in candles if c.close_time < now_ms]
                    step = {"5m": 300000, "15m": 900000, "1h": 3600000, "4h": 14400000, "1d": 86400000}[settings.timeframe]
                    if not closed or now_ms - closed[-1].close_time > step + 60_000:
                        raise ValueError("Stale candle data")
                    f = features(closed, market, settings)
                    computed = score(f, settings)
                    history = self.database.history(market.symbol, settings.timeframe, since=timestamp-timedelta(hours=max(settings.momentum_windows), minutes=settings.history_tolerance_minutes), limit=10000)
                    # Config/score version changes must not masquerade as market momentum.
                    with self.database.session() as session:
                        from crypto_bot.storage.models import ScannerRun
                        compatible = {h.run_id for h in history if session.get(ScannerRun, h.run_id).configuration == settings.public_dict() and h.score_version == computed["score_version"]}
                    momentum = score_momentum(computed["total_score"], timestamp, [h for h in history if h.run_id in compatible], settings.momentum_windows, settings.history_tolerance_minutes)
                    computed["reasons"] += [f"score {momentum[f'delta_{w}h']:+.1f} in last {w}h" for w in settings.momentum_windows if momentum[f"delta_{w}h"] is not None]
                    rows.append(dict(symbol=market.symbol, features=f, momentum=momentum, auto_eligible=market in candidates, **computed))
                except Exception as exc:
                    errors[market.symbol] = str(exc)
                    log.warning("INSTRUMENT_ERROR", extra={"event_data": {"symbol": market.symbol, "error": str(exc)}})
            selected = select_watchlist(rows, settings)
            self.database.finish_run(run_id, markets, rows, selected, errors, len(candidates))
            added, removed = set(selected)-previous, previous-set(selected)
            log.info("AUTO_WATCHLIST_CHANGE", extra={"event_data": {"added": {s: selected[s] for s in added}, "removed": {s: "No longer eligible or below selection rank" for s in removed}}})
            log.info("SCANNER_COMPLETE", extra={"event_data": {"run_id": run_id, "discovered": len(markets), "passed_filters": sum(eligible(m, settings) for m in markets), "candidates": len(candidates), "scored": len(rows), "errors": errors, "top_score": [(r["symbol"], round(r["total_score"], 2)) for r in sorted(rows, key=lambda r: -r["total_score"])[:settings.top_score_size]], "top_risers": [(r["symbol"], r["momentum"][f"delta_{settings.riser_window}h"]) for r in sorted((r for r in rows if (r["momentum"].get(f"delta_{settings.riser_window}h") or 0) > 0), key=lambda r: -r["momentum"][f"delta_{settings.riser_window}h"])[:settings.top_risers_size]]}})
            return rows
        except Exception as exc:
            self.database.fail_run(run_id, exc)
            log.exception("SCANNER_FAILED", extra={"event_data": {"run_id": run_id, "error": str(exc)}})
            raise
