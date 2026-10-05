import math

OVERRIDES = {"AUTO", "WATCH", "PINNED", "IGNORE"}


def eligible(market, settings, manual=False):
    if not market.active or not market.spot:
        return False
    if manual:
        return True  # observation only; automatic liquidity gates still apply to selection
    return (market.quote_asset == settings.quote_asset and market.base_asset not in settings.stable_assets and market.base_asset not in settings.excluded_assets and not any(market.base_asset.endswith(s) for s in settings.leveraged_suffixes) and math.isfinite(market.quote_volume) and market.quote_volume >= settings.minimum_quote_volume and math.isfinite(market.price_change_pct) and market.spread_pct <= settings.maximum_spread)


def universe(markets, settings):
    return sorted((m for m in markets if eligible(m, settings)), key=lambda m: (-m.quote_volume, m.symbol))[:settings.number_of_markets]


def effective_state(algorithm_watch, override):
    if override not in OVERRIDES:
        raise ValueError("Invalid override")
    return override if override != "AUTO" else "WATCH" if algorithm_watch else "AUTO"


def select_watchlist(rows, settings):
    candidates = [r for r in rows if r["auto_eligible"] and r["total_score"] >= settings.auto_min_score and r["features"]["relative_volume"] >= settings.auto_min_relative_volume and settings.auto_min_atr_pct <= r["features"]["atr_pct"] <= settings.auto_max_atr_pct]
    def priority(row):
        delta = row["momentum"].get(f"delta_{settings.riser_window}h")
        return row["total_score"] + settings.selection_momentum_weight * max(0, delta or 0)
    selected = sorted(candidates, key=lambda r: (-priority(r), r["symbol"]))[:settings.auto_watchlist_size]
    return {r["symbol"]: f"score {r['total_score']:.1f}; Δ{settings.riser_window}h {r['momentum'].get(f'delta_{settings.riser_window}h')}; selection rank {i+1}" for i, r in enumerate(selected)}
