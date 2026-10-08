"""Strict immutable run snapshots; never reads PaperSettings.load or production env."""

import copy
import json
import re
from dataclasses import fields, replace
from datetime import datetime, timezone
from decimal import DecimalException
from pathlib import Path

from crypto_bot.config.settings import Settings
from crypto_bot.replay.clock import ms, utc
from crypto_bot.replay.registry import EXITS, STRATEGIES
from crypto_bot.replay.stale import POLICIES, STRICT
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


INPUT_FIELDS = (
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
    "stale_position_policy",
)
VARIANT_FIELDS = (
    "name",
    "strategy",
    "strategy_parameters",
    "exit_policy",
    "exit_parameters",
    "risk_parameters",
)


class ConfigValidationError(ValueError):
    def __init__(self, errors):
        self.errors = errors
        super().__init__("; ".join(f"{e['field']}: {e['message']}" for e in errors))


def invalid(field, message):
    raise ConfigValidationError([dict(field=field, message=message)])


def at_field(field, callback):
    try:
        return callback()
    except ConfigValidationError:
        raise
    except (
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
        DecimalException,
        OverflowError,
    ) as exc:
        message = (
            "Must be a finite number" if isinstance(exc, DecimalException) else str(exc)
        )
        invalid(field, message or "Invalid value")


def object_fields(value, allowed, path):
    if not isinstance(value, dict):
        invalid(path, "Must be a JSON object")
    unknown = sorted(set(value) - set(allowed))
    if unknown:
        raise ConfigValidationError(
            [
                dict(field=f"{path}.{k}" if path != "$" else k, message="Unknown field")
                for k in unknown
            ]
        )


def input_config(snapshot):
    """The existing POST request schema, with all effective defaults explicit.

    Stored run snapshots also contain derived engine metadata. That internal
    metadata is deliberately not another user import format.
    """
    result = {
        k: copy.deepcopy(
            snapshot.get(k, STRICT) if k == "stale_position_policy" else snapshot[k]
        )
        for k in INPUT_FIELDS
    }
    result["variants"] = [
        {k: copy.deepcopy(v[k]) for k in VARIANT_FIELDS} for v in snapshot["variants"]
    ]
    return result


def converted(default, values, allowed, path="parameters"):
    object_fields(values, allowed, path)
    out, errors = {}, []
    for k, v in values.items():

        def convert():
            old = getattr(default, k)
            if isinstance(old, bool):
                if type(v) is not bool:
                    raise ValueError("Must be boolean")
                return v
            if isinstance(old, int):
                if type(v) is not int:
                    raise ValueError("Must be integer")
                return v
            if isinstance(old, str):
                if not isinstance(v, str):
                    raise ValueError("Must be string")
                return v
            return float(v) if isinstance(old, float) else D(v)

        try:
            out[k] = at_field(f"{path}.{k}", convert)
        except ConfigValidationError as exc:
            errors.extend(exc.errors)
    if errors:
        raise ConfigValidationError(errors)
    return out


def variant(value, index=0):
    path = f"variants[{index}]"
    object_fields(value, VARIANT_FIELDS, path)
    errors = []
    try:
        definition = at_field(
            f"{path}.strategy",
            lambda: STRATEGIES.get(value.get("strategy", "watchlist_reference_v1")),
        )
    except ConfigValidationError as exc:
        errors.extend(exc.errors)
    policy = value.get("exit_policy", "baseline_v1")
    try:
        defaults = at_field(f"{path}.exit_policy", lambda: EXITS.get(policy))
    except ConfigValidationError as exc:
        errors.extend(exc.errors)
    if errors:
        raise ConfigValidationError(errors)
    params = value.get("exit_parameters", {})
    object_fields(params, defaults, f"{path}.exit_parameters")
    params = defaults | params
    params = {
        k: at_field(f"{path}.exit_parameters.{k}", lambda v=v: str(D(v)))
        for k, v in params.items()
    }
    for k, v in params.items():
        if D(v) < 0 or D(v) > 100 or (k != "buffer_pct" and D(v) == 0):
            invalid(
                f"{path}.exit_parameters.{k}",
                "Must be between 0 and 100; only buffer_pct may be zero",
            )
    for key in ("distance_pct", "tp_pct"):
        if key in params and D(params[key]) >= 100:
            invalid(f"{path}.exit_parameters.{key}", "Percentage must be below 100")
    if policy == "profit_lock" and D(params["lock_pct"]) >= D(params["activation_pct"]):
        invalid(f"{path}.exit_parameters.lock_pct", "Must be below activation_pct")
    paper = PaperSettings()
    values, errors = {}, []
    for group, allowed in (
        ("risk_parameters", RISK_NAMES),
        ("strategy_parameters", definition.parameters),
    ):
        try:
            values.update(
                converted(paper, value.get(group, {}), allowed, f"{path}.{group}")
            )
        except ConfigValidationError as exc:
            errors.extend(exc.errors)
    if errors:
        raise ConfigValidationError(errors)
    try:
        paper = replace(paper, **values)
    except ValueError as exc:
        message = str(exc)
        names = [
            f.name for f in fields(paper) if re.search(r"\b" + f.name + r"\b", message)
        ]
        if message == "Invalid strategy/slippage thresholds":
            names = [
                k
                for k in (
                    "min_rsi",
                    "max_rsi",
                    "min_score",
                    "exit_score",
                    "paper_slippage_pct",
                )
                if k in values
            ]
        if "Pyramiding" in message:
            names = ["pyramiding"]
        raise ConfigValidationError(
            [
                dict(
                    field=f"{path}.{'risk_parameters' if k in RISK_NAMES else 'strategy_parameters'}.{k}",
                    message=message,
                )
                for k in names
            ]
            or [dict(field=path, message=message)]
        ) from exc
    if paper.delta_window not in (1, 4, 24):
        invalid(f"{path}.strategy_parameters.delta_window", "Must be 1, 4 or 24")
    if paper.max_open_positions > 100:
        invalid(f"{path}.risk_parameters.max_open_positions", "Must be at most 100")
    name = value.get("name", f"Variant {index + 1}")
    if not isinstance(name, str) or not 1 <= len(name) <= 80:
        invalid(f"{path}.name", "Must be a string of 1..80 characters")
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
    return _validate(value)


def _validate(value):
    object_fields(value, INPUT_FIELDS, "$")
    stale_policy = value.get("stale_position_policy", STRICT)
    if stale_policy not in POLICIES:
        invalid(
            "stale_position_policy",
            "Must be STRICT_FRESH_MARKS or RESEARCH_QUARANTINE_STALE",
        )
    dates, errors = {}, []
    for key in ("start", "end"):

        def date():
            if key not in value:
                raise ValueError("Required")
            if not isinstance(value[key], str):
                raise ValueError("Must be an ISO-8601 date/time string")
            return utc(value[key])

        try:
            dates[key] = at_field(key, date)
        except ConfigValidationError as exc:
            errors.extend(exc.errors)
    if errors:
        raise ConfigValidationError(errors)
    start, end = dates["start"], dates["end"]
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    if start < datetime(2017, 8, 1):
        invalid("start", "Must be August 2017 or later")
    if end <= start:
        invalid("end", "Must be after start (exclusive end)")
    if end > now:
        invalid("end", "Must be in the past")
    if (end - start).days > 1096:
        invalid("end", "Period must be at most three years")
    for key, date in dates.items():
        if ms(date) % 3600000:
            invalid(key, "Use whole UTC hours; end is exclusive")
    top = value.get("universe_size", 50)
    if type(top) is not int or not 1 <= top <= 200:
        invalid("universe_size", "Must be an integer between 1 and 200")
    role = value.get("dataset_role", "EXPLORATION")
    if role not in ("EXPLORATION", "VALIDATION", "OUT_OF_SAMPLE"):
        invalid("dataset_role", "Must be EXPLORATION, VALIDATION or OUT_OF_SAMPLE")
    for k in ("fallback_5m", "conservative"):
        if k in value and type(value[k]) is not bool:
            invalid(k, "Must be boolean")
    raw = value.get("variants", [{}])
    if not isinstance(raw, list) or not 1 <= len(raw) <= 10:
        invalid("variants", "Choose 1..10 variants")

    candidates = value.get("candidate_symbols")
    if candidates is not None and (
        not isinstance(candidates, list)
        or not 1 <= len(candidates) <= 1500
        or any(not valid_symbol(s) for s in candidates)
    ):
        invalid(
            "candidate_symbols", "Must be null or 1..1500 valid USDT symbol strings"
        )
    minimum = at_field(
        "minimum_quote_volume", lambda: D(value.get("minimum_quote_volume", 5000000))
    )
    spread = at_field(
        "spread_approximation_pct",
        lambda: D(value.get("spread_approximation_pct", "0.02")),
    )
    if minimum < 0:
        invalid("minimum_quote_volume", "Must be nonnegative")
    if not 0 <= spread <= D(".2"):
        invalid("spread_approximation_pct", "Must be between 0 and 0.2")
    scanner = at_field(
        "minimum_quote_volume",
        lambda: Settings(number_of_markets=top, minimum_quote_volume=float(minimum)),
    )
    variants, errors = [], []
    for i, v in enumerate(raw):
        try:
            variants.append(variant(v, i))
        except ConfigValidationError as exc:
            errors.extend(exc.errors)
    if errors:
        raise ConfigValidationError(errors)
    scanner_snapshot = scanner.public_dict()
    for k in ("database_url", "secret_key", "api_url"):
        scanner_snapshot.pop(k, None)
    return dict(
        schema_version=1,
        stale_position_policy=stale_policy,
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
        variants=variants,
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
