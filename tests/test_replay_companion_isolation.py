"""Companion invariance is a required research-engine invariant, not a strategy test."""

import json
from collections import defaultdict
from copy import deepcopy
from dataclasses import FrozenInstanceError

import pytest
from test_replay import Synthetic, config, row

from crypto_bot.data.base import Candle, Market
from crypto_bot.replay.clock import ms
from crypto_bot.replay.config import input_config, validate
from crypto_bot.replay.engine import ReplayEngine
from crypto_bot.replay.institutional import InstitutionalContext
from crypto_bot.replay.market_context import (
    ISOLATED,
    LEGACY,
    canonical_scan_histories,
    frame,
)
from crypto_bot.replay.models import ReplayStore
from crypto_bot.replay.universe import CandleHistory

START = ms("2024-01-01")
H = 3600000


class Tape(Synthetic):
    def catalog(self):
        return ["NEARUSDT", "OPUSDT"]

    def hourly(self, symbol, start, end):
        return [
            (
                Candle(
                    t,
                    t + H - 1,
                    300 + (t - START) / H * 0.002,
                    301 + (t - START) / H * 0.002,
                    299 + (t - START) / H * 0.002,
                    300 + (t - START) / H * 0.002,
                    10,
                ),
                10000000.125,
            )
            for t in range(start, end, H)
        ]

    def minutes(self, symbol, start, end, interval="1m"):
        return {
            t: dict(
                time=t,
                close_time=t + 59999,
                open=100,
                high=100.1,
                low=97 if start >= START + 11 * H else 99.9,
                close=100,
                interval="1m",
            )
            for t in range(start, end, 60000)
        }


class RotatingUniverse:
    def __init__(self, histories, settings, spread):
        self.histories = histories

    allowed = staticmethod(lambda symbol, settings: True)

    def as_of(self, symbol, now):
        return [(c, v) for c, v in self.histories[symbol] if c.close_time < now][-499:]

    def market(self, symbol, now):
        return Market(symbol, symbol[:-4], "USDT", True, True, 1e8, 99.99, 100.01, 1)

    def ranked(self, now):
        symbols = ["NEARUSDT"]
        if now < START + H or now >= START + 6 * H:
            symbols.append("OPUSDT")
        return [self.market(s, now) for s in symbols]


def features(candles, market, settings):
    now = candles[-1].close_time + 1
    f = dict(row()["features"])
    f.update(
        price=100,
        ema50=101,
        ema200=100,
        atr=1,
        atr_pct=1,
        rsi=55 if market.symbol == "NEARUSDT" else (85 if now < START + 6 * H else 60),
        relative_volume=1.5,
        range_position=0.5,
        momentum=1,
    )
    f["test_score"] = (
        (76.4 if now >= START + 6 * H else 73.8)
        if market.symbol == "NEARUSDT"
        else (
            77.0
            if START <= now < START + H
            else (75.6 if now >= START + 6 * H else 69.7)
        )
    )
    return f


def score(f, s):
    return dict(
        total_score=f["test_score"],
        components={"fixed": f["test_score"]},
        reasons=["fixed source"],
    )


@pytest.fixture
def controlled(monkeypatch):
    import crypto_bot.replay.engine as engine

    monkeypatch.setattr(engine, "HistoricalUniverse", RotatingUniverse)
    monkeypatch.setattr(engine, "features", features)
    monkeypatch.setitem(engine.SCORES.items, "heuristic-v1", score)


def definitions():
    common = dict(
        strategy="institutional_style_v1",
        strategy_parameters=dict(correlation_filter_enabled=False),
        risk_parameters=dict(daily_loss_limit_pct=100, max_drawdown_limit_pct=100),
    )
    defs = {
        name: common | dict(name=name, exit_policy=policy)
        for name, policy in zip(
            "ABCDE",
            [
                "baseline_v1",
                "adaptive_peak_lock_v1",
                "break_even_guard_v1",
                "delayed_peak_lock_v2",
                "delayed_peak_lock_v2",
            ],
        )
    }
    defs["E"] = defs["E"] | dict(setup_rearm="setup_rearm_v1")
    # Real companion BUY/hold of OP outside TopN; NEAR RSI rejects its entry.
    defs["X"] = dict(
        name="X",
        strategy="watchlist_reference_v1",
        strategy_parameters=dict(
            min_rsi=80,
            max_rsi=90,
            require_watch=False,
            require_delta=False,
            min_score=60,
            exit_score=0,
        ),
        risk_parameters=dict(daily_loss_limit_pct=100, max_drawdown_limit_pct=100),
    )
    defs["LONG"] = common | dict(
        name="LONG",
        exit_policy="baseline_v1",
        strategy_parameters=dict(
            correlation_filter_enabled=False,
            ema_slow_period=300,
            min_rsi=80,
            max_rsi=90,
            require_watch=False,
            require_delta=False,
            min_score=60,
            exit_score=0,
        ),
    )
    return defs


def execute(tmp_path, names, mode=ISOLATED, stop=None):
    defs = definitions()
    raw = input_config(config())
    raw.update(
        start="2024-01-01T00:00:00Z",
        end="2024-01-01T14:00:00Z",
        candidate_symbols=["NEARUSDT", "OPUSDT"],
        universe_size=20,
        stale_position_policy="RESEARCH_QUARANTINE_STALE",
        variants=[defs[n] for n in names],
    )
    cfg = validate(raw)
    cfg["scanner_settings"]["auto_watchlist_size"] = 1
    cfg["market_context_version"] = mode
    store = ReplayStore(tmp_path / "replay.db")
    rid = store.create(cfg)

    def check():
        if stop:
            point = store.checkpoint(rid)
            if point and point["next_time"] >= stop:
                raise InterruptedError("intentional checkpoint")

    try:
        ReplayEngine(store, Tape(), check).run(rid)
    except InterruptedError:
        pass
    return store, rid


def core(store, rid, index):
    trades, curve = store.data(rid, index)
    with store.connection() as db:
        orders = [
            json.loads(r[0])
            for r in db.execute(
                "select data from replay_orders where run_id=? and variant=? order by id",
                (rid, index),
            )
        ]
        fills = [
            json.loads(r[0])
            for r in db.execute(
                "select data from replay_fills where run_id=? and variant=? order by id",
                (rid, index),
            )
        ]
    m = store.get(rid)["variants"][index]["summary"]["metrics"]
    return dict(
        trades=trades,
        curve=curve,
        orders=orders,
        fills=fills,
        cash=m["cash"],
        equity=m["final_equity"],
    )


@pytest.mark.parametrize("control", ["A", "B"])
@pytest.mark.parametrize(
    "companions",
    [
        ["X"],
        ["A", "B"],
        ["A", "B", "C"],
        list("ABCDE"),
        ["LONG"],
        ["E", "D", "X", "B", "A", "C"],
    ],
)
def test_required_control_companion_invariant(
    tmp_path, controlled, control, companions
):
    solo, r1 = execute(tmp_path / "solo", [control])
    a = core(solo, r1, 0)
    assert a["trades"] and a["fills"]
    names = list(dict.fromkeys([control] + companions))
    mixed, r2 = execute(tmp_path / "mixed", names)
    assert a == core(mixed, r2, names.index(control))


def test_legacy_fixture_exposes_real_cross_variant_coupling(tmp_path, controlled):
    solo, r1 = execute(tmp_path / "solo", ["A"], LEGACY)
    mixed, r2 = execute(tmp_path / "mixed", ["A", "X"], LEGACY)
    a = core(solo, r1, 0)
    b = core(mixed, r2, 0)
    assert a["trades"][0]["symbol"] == "NEARUSDT"
    assert b["trades"][0]["symbol"] == "OPUSDT"
    assert a["orders"] != b["orders"]


def test_immutable_frame_caller_copies_and_exit_history(tmp_path, controlled):
    universe = RotatingUniverse(
        {
            "NEARUSDT": CandleHistory(
                Tape().hourly("NEARUSDT", START - 524 * H, START + H)
            )
        },
        None,
        0,
    )
    history = defaultdict(list)
    # Rank only NEAR at this decision timestamp.
    f = frame(
        universe,
        __import__("crypto_bot.config.settings", fromlist=["Settings"]).Settings(),
        START + 2 * H,
        history,
        score,
        features,
    )
    with pytest.raises(FrozenInstanceError):
        f.btc_regime = "BAD"
    copies = f.rows()
    copies[0]["features"]["price"] = 999
    watch = f.watch()
    watch.clear()
    assert f.rows()[0]["features"]["price"] == 100 and "NEARUSDT" in history


def test_checkpoint_resume_bit_exact_and_mode_guard(tmp_path, controlled):
    full, r1 = execute(tmp_path / "full", list("ABCDE"))
    resumed, r2 = execute(tmp_path / "resumed", list("ABCDE"), stop=START + 8 * H)
    checkpoint = resumed.checkpoint(r2)
    assert checkpoint["market_context_version"] == ISOLATED
    ReplayEngine(resumed, Tape()).run(r2)
    for i in range(5):
        assert core(full, r1, i) == core(resumed, r2, i)
    other, r3 = execute(tmp_path / "other", ["A"], stop=START + 8 * H)
    with other.connection() as db:
        cfg = json.loads(
            db.execute("select config from replay_runs where id=?", (r3,)).fetchone()[0]
        )
        cfg.pop("market_context_version")
        db.execute("update replay_runs set config=? where id=?", (json.dumps(cfg), r3))
    with pytest.raises(ValueError, match="different market context"):
        ReplayEngine(other, Tape()).run(r3)


def test_own_ema_seed_independent_of_companion_raw_warmup():
    tape = Tape()
    end = START + H
    short = {"BTCUSDT": CandleHistory(tape.hourly("BTCUSDT", START - 4824 * H, end))}
    long = {"BTCUSDT": CandleHistory(tape.hourly("BTCUSDT", START - 7224 * H, end))}
    a = InstitutionalContext(short, START).trend("BTCUSDT", 24, START, 50, 200)
    b = InstitutionalContext(long, START).trend("BTCUSDT", 24, START, 50, 200)
    assert a == b and a["observations"] == 201
    assert InstitutionalContext(short).trend(
        "BTCUSDT", 24, START, 50, 200
    ) != InstitutionalContext(long).trend("BTCUSDT", 24, START, 50, 200)
    s = canonical_scan_histories(short, START - 524 * H)
    longer = canonical_scan_histories(long, START - 524 * H)
    assert list(s["BTCUSDT"]) == list(longer["BTCUSDT"])


def test_reference_reporting_is_not_companion_dependent(tmp_path, controlled):
    solo, r1 = execute(tmp_path / "solo", ["X"])
    mixed, r2 = execute(tmp_path / "mixed", ["X", "A", "B", "C", "D", "E"])
    assert core(solo, r1, 0) == core(mixed, r2, 0)
    assert core(solo, r1, 0)["trades"]


def test_mutating_companion_cannot_mutate_another_frame(
    tmp_path, controlled, monkeypatch
):
    import crypto_bot.replay.engine as engine

    original = engine.Variant.signals

    def hostile(self, rows, watch, now):
        if self.config["name"] == "X":
            for r in rows:
                r["features"]["price"] = 999999
            watch.clear()
            rows.clear()
            self.pending = []
            return set()
        return original(self, rows, watch, now)

    solo, r1 = execute(tmp_path / "solo", ["A", "B"])
    monkeypatch.setattr(engine.Variant, "signals", hostile)
    mixed, r2 = execute(tmp_path / "mixed", ["X", "A", "B"])
    for i in range(2):
        assert core(solo, r1, i) == core(mixed, r2, i + 1)


def test_future_candles_do_not_affect_current_frame():
    from crypto_bot.config.settings import Settings

    source = CandleHistory(Tape().hourly("NEARUSDT", START - 524 * H, START + 14 * H))
    universe = RotatingUniverse({"NEARUSDT": source}, None, 0)
    first = frame(
        universe, Settings(), START + 2 * H, defaultdict(list), score, features
    )
    # Entire future OHLC changed; only strictly closed as-of candles may be used.
    from dataclasses import replace

    changed = CandleHistory(
        [
            (
                replace(c, close=99999, high=99999)
                if c.open_time >= START + 2 * H
                else c,
                v,
            )
            for c, v in source
        ]
    )
    second = frame(
        RotatingUniverse({"NEARUSDT": changed}, None, 0),
        Settings(),
        START + 2 * H,
        defaultdict(list),
        score,
        features,
    )
    assert first == second


def test_config_version_is_derived_and_import_export_keeps_schema():
    from crypto_bot.replay.config import ConfigValidationError, config_hash

    snapshot = config()
    assert snapshot["market_context_version"] == ISOLATED
    public = input_config(snapshot)
    assert "market_context_version" not in public
    assert validate(public) == snapshot
    legacy = deepcopy(snapshot)
    legacy.pop("market_context_version")
    before = config_hash(legacy)
    assert before != config_hash(snapshot) and "market_context_version" not in legacy
    with pytest.raises(ConfigValidationError):
        validate(public | {"market_context_version": LEGACY})


def test_legacy_checkpoint_resume_does_not_upgrade_context(tmp_path, controlled):
    full, r1 = execute(tmp_path / "full", ["A", "X"], LEGACY)
    resumed, r2 = execute(tmp_path / "resumed", ["A", "X"], LEGACY, stop=START + 8 * H)
    assert "market_context_version" not in resumed.checkpoint(r2)
    # Existing stored configurations without a marker remain legacy on resume.
    with resumed.connection() as db:
        cfg = json.loads(
            db.execute("select config from replay_runs where id=?", (r2,)).fetchone()[0]
        )
        cfg.pop("market_context_version")
        db.execute("update replay_runs set config=? where id=?", (json.dumps(cfg), r2))
    ReplayEngine(resumed, Tape()).run(r2)
    for i in range(2):
        assert core(full, r1, i) == core(resumed, r2, i)


def test_reporting_diagnostics_skip_missing_metrics_but_keep_legacy_reports():
    from crypto_bot.replay.exit_v2 import evaluation

    configs = [
        {"strategy": "watchlist_reference_v1", "exit_policy": "baseline_v1"},
        {"strategy": "institutional_style_v1", "exit_policy": "baseline_v1"},
    ]
    metrics = dict(trade_count=2, turnover_notional="100", fees="1", return_pct=0)
    # New isolated Reference reporting has no companion's research enrichment.
    result = evaluation(
        configs, [{"metrics": {"trade_count": 2}}, {"metrics": metrics}]
    )
    assert result["warnings"] == []
    # Legacy mixed runs retain their original enriched reporting diagnostics.
    legacy = evaluation(configs, [{"metrics": metrics}, {"metrics": metrics}])
    assert legacy["warnings"] == [{"variant": 0, "control": 1, "warnings": []}]
