"""Strict immutable run snapshots; never reads PaperSettings.load or production env."""

import copy
import json
from dataclasses import fields, replace
from datetime import datetime, timezone
from pathlib import Path

from crypto_bot.config.settings import Settings
from crypto_bot.replay.clock import ms, utc
from crypto_bot.replay.registry import EXITS, STRATEGIES
from crypto_bot.replay.symbols import valid_symbol
from crypto_bot.trading.config import PaperSettings
from crypto_bot.trading.domain import D

RISK_NAMES = (
    "initial_paper_balance",
    "risk_per_trade_pct",
    "max_open_positions",
    "max_total_exposure_pct",
    "max_symbol_exposure_pct",
    "daily_loss_limit_pct",
    "max_drawdown_limit_pct",
    "minimum_order_value",
    "paper_fee_pct",
    "paper_slippage_pct",
    "pyramiding",
)


def converted(default, values, allowed):
    if not isinstance(values, dict) or set(values) - set(allowed):
        raise ValueError("Unknown configuration field")
    out = {}
    for k, v in values.items():
        old = getattr(default, k)
        if isinstance(old, bool):
            if type(v) is not bool:
                raise ValueError(f"{k} must be boolean")
            out[k] = v
        elif isinstance(old, int):
            if type(v) is not int:
                raise ValueError(f"{k} must be integer")
            out[k] = v
        elif isinstance(old, str):
            if not isinstance(v, str):
                raise ValueError(f"{k} must be string")
            out[k] = v
        elif isinstance(old, float):
            out[k] = float(v)
        else:
            out[k] = D(v)
    return out


def variant(value, index=0):
    if not isinstance(value, dict) or set(value) - {
        "name",
        "strategy",
        "strategy_parameters",
        "exit_policy",
        "exit_parameters",
        "risk_parameters",
    }:
        raise ValueError("Unknown variant field")
    definition = STRATEGIES.get(value.get("strategy", "watchlist_reference_v1"))
    policy = value.get("exit_policy", "baseline_v1")
    defaults = EXITS.get(policy)
    params = value.get("exit_parameters", {})
    if not isinstance(params, dict) or set(params) - set(defaults):
        raise ValueError("Unknown exit parameter")
    params = defaults | params
    params = {k: str(D(v)) for k, v in params.items()}
    for k, v in params.items():
        if D(v) < 0 or D(v) > 100 or (k != "buffer_pct" and D(v) == 0):
            raise ValueError("Invalid exit parameter")
    if any(D(params[k]) >= 100 for k in ("distance_pct", "tp_pct") if k in params):
        raise ValueError("Invalid percentage exit parameter")
    if policy == "profit_lock" and D(params["lock_pct"]) >= D(params["activation_pct"]):
        raise ValueError("Profit lock must be below activation")
    paper = PaperSettings()
    paper = replace(
        paper,
        **converted(paper, value.get("risk_parameters", {}), RISK_NAMES),
        **converted(paper, value.get("strategy_parameters", {}), definition.parameters),
    )
    if paper.delta_window not in (1, 4, 24) or paper.max_open_positions > 100:
        raise ValueError("Unsupported delta window or position count")
    name = value.get("name", f"Variant {index + 1}")
    if not isinstance(name, str) or not 1 <= len(name) <= 80:
        raise ValueError("Invalid variant name")
    snapshot = paper.public_dict()
    return dict(
        name=name,
        strategy=definition.name,
        strategy_version=definition.version,
        strategy_parameters={k: snapshot[k] for k in definition.parameters},
        exit_policy=policy,
        exit_parameters=params,
        risk_parameters={k: snapshot[k] for k in RISK_NAMES},
        paper_settings=snapshot,
    )


def settings_for(value):
    default = PaperSettings()
    raw = value["paper_settings"]
    return replace(
        default, **converted(default, raw, [f.name for f in fields(default)])
    )


def validate(value):
    if not isinstance(value, dict) or set(value) - {
        "start",
        "end",
        "universe_size",
        "variants",
        "dataset_role",
        "fallback_5m",
        "conservative",
        "minimum_quote_volume",
        "spread_approximation_pct",
        "candidate_symbols",
        "name",
    }:
        raise ValueError("Unknown replay configuration field")
    start, end = utc(value["start"]), utc(value["end"])
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    if (
        start < datetime(2017, 8, 1)
        or end <= start
        or end > now
        or (end - start).days > 1096
    ):
        raise ValueError(
            "Historical period must be in the past, positive, at most three years"
        )
    if ms(start) % 3600000 or ms(end) % 3600000:
        raise ValueError("Use whole UTC hours; end is exclusive")
    top = value.get("universe_size", 50)
    if type(top) is not int or not 1 <= top <= 200:
        raise ValueError("Universe size must be 1..200")
    role = value.get("dataset_role", "EXPLORATION")
    if role not in ("EXPLORATION", "VALIDATION", "OUT_OF_SAMPLE"):
        raise ValueError("Invalid dataset role")
    for k in ("fallback_5m", "conservative"):
        if k in value and type(value[k]) is not bool:
            raise ValueError("Expected boolean execution parameter")
    raw = value.get("variants", [{}])
    if not isinstance(raw, list) or not 1 <= len(raw) <= 10:
        raise ValueError("Choose 1..10 variants")

    candidates = value.get("candidate_symbols")
    if candidates is not None and (
        not isinstance(candidates, list)
        or not 1 <= len(candidates) <= 1500
        or any(not valid_symbol(s) for s in candidates)
    ):
        raise ValueError("Invalid explicit research universe")
    minimum = D(value.get("minimum_quote_volume", 5000000))
    spread = D(value.get("spread_approximation_pct", "0.02"))
    if minimum < 0 or not 0 <= spread <= D(".2"):
        raise ValueError("Invalid historical volume/spread model")
    scanner = Settings(number_of_markets=top, minimum_quote_volume=float(minimum))
    scanner_snapshot = scanner.public_dict()
    for k in ("database_url", "secret_key", "api_url"):
        scanner_snapshot.pop(k, None)
    return dict(
        schema_version=1,
        name=str(value.get("name", "Historical Replay"))[:80],
        start=start.isoformat() + "Z",
        end=end.isoformat() + "Z",
        timeframe="1h",
        warmup_hours=524,
        universe_size=top,
        universe_method="ROLLING_24H_QUOTE_VOLUME_APPROXIMATION",
        candidate_symbols=sorted(set(candidates)) if candidates else None,
        candidate_method="EXPLICIT_RESEARCH_SET"
        if candidates
        else "BINANCE_ARCHIVE_CATALOG",
        minimum_quote_volume=str(minimum),
        spread_approximation_pct=str(spread),
        spread_model="CONFIGURED_CONSTANT_APPROXIMATION_NOT_HISTORICAL_BOOK",
        score_version="heuristic-v1",
        score_weights=copy.deepcopy(scanner.weights),
        scanner_settings=scanner_snapshot,
        variants=[variant(v, i) for i, v in enumerate(raw)],
        dataset_role=role,
        fallback_5m=value.get("fallback_5m", False),
        conservative=value.get("conservative", True),
        random_seed=None,
        execution_source="BINANCE_PUBLIC_KLINES_APPROXIMATION",
    )


def isolated_paths(root, production=None):
    root = Path(root).resolve()
    database, cache = root / "replay.db", root / "replay-cache"
    if database.is_symlink() or cache.is_symlink():
        raise ValueError("Replay paths cannot be symlinks")
    if production or (root / "market.db").exists():
        p = Path(production or root / "market.db").resolve()
        if database.resolve() == p or (
            p.exists() and database.exists() and p.samefile(database)
        ):
            raise ValueError("Replay database must be separate from production")
    root.mkdir(parents=True, exist_ok=True)
    return database, cache


def config_hash(value):
    import hashlib

    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
