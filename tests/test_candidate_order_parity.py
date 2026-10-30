"""Small fixed decision batches: real engines/ledgers, no historical replay or HTTP."""

from copy import deepcopy
from datetime import timedelta
from itertools import permutations

import pytest
from conftest import NOW, market
from sqlalchemy import select
from test_paper_quotes_permissions import FixedQuotes
from test_replay import config, row

from crypto_bot.replay.clock import ms
from crypto_bot.replay.config import settings_for
from crypto_bot.replay.engine import Variant
from crypto_bot.storage.migrate import upgrade_database
from crypto_bot.storage.paper_models import PaperOrder, PaperPosition, SignalRecord
from crypto_bot.trading.domain import D
from crypto_bot.trading.engine import PaperEngine
from crypto_bot.trading.ordering import CANDIDATE_ORDER_POLICY, candidate_order_key
from crypto_bot.trading.repository import PaperRepository


@pytest.fixture
def parity_input():
    """Engine-independent decision input, including occupancy and capacity."""
    return dict(
        timestamp=NOW,
        candidates=[
            dict(symbol="AAAUSDT", score=75, action="BUY"),
            dict(symbol="BBBUSDT", score=90, action="BUY"),
            dict(symbol="CCCUSDT", score=80, action="BUY"),
        ],
        existing_positions=[],
        max_open_positions=1,
    )


def rows_for(candidates):
    return [
        row()
        | dict(
            symbol=c["symbol"],
            total_score=c["score"],
            score_version="fixture-v1",
            features=row()["features"] | {"spread": 0.01},
            components=dict(
                trend=25, momentum=17, volume=16, volatility=12, liquidity=12
            ),
        )
        for c in candidates
    ]


class Pair:
    def __init__(self, database, settings, data):
        self.database, self.scanner, self.data = database, settings, data
        variant_config = config(
            variants=[
                dict(
                    risk_parameters=dict(max_open_positions=data["max_open_positions"])
                )
            ]
        )["variants"][0]
        self.replay = Variant(variant_config, ms(NOW))
        cfg = settings_for(variant_config)
        upgrade_database(database)
        self.repo = PaperRepository(database, cfg)
        self.repo.initialize_account(NOW)
        self.repo.set_enabled(True, NOW)
        self.quotes = FixedQuotes()
        self.quotes.quotes = {}
        self.paper = PaperEngine(self.repo, settings, quote_provider=self.quotes)

    def seed(self, candidates, when):
        rows = rows_for(candidates)
        run = self.database.start_run(self.scanner, when)
        self.database.finish_run(
            run,
            [
                market(symbol=r["symbol"], base=r["symbol"].removesuffix("USDT"))
                for r in rows
            ],
            rows,
            {r["symbol"]: "ordering fixture" for r in rows},
            {},
            1,
        )
        return rows

    def batch(self, candidates, when=NOW, missing=None, references=None):
        references = references or {}
        rows = self.seed(candidates, when)
        for r in rows:
            self.quotes.set(
                references.get(r["symbol"], 100), when=when, symbol=r["symbol"]
            )
        self.quotes.quotes.pop(missing, None)
        datasets = {
            r["symbol"]: {
                ms(when): dict(open=references.get(r["symbol"], 100), interval="1m")
            }
            for r in rows
            if r["symbol"] != missing
        }
        self.paper.run_once(when)
        self.replay.signals(rows, {r["symbol"] for r in rows}, ms(when))
        self.last_pending = list(self.replay.pending)
        self.replay.execute_signals(datasets, ms(when), False)

    def symbols(self):
        with self.database.session() as s:
            paper = [
                p.symbol
                for p in s.scalars(
                    select(PaperPosition)
                    .where(PaperPosition.status == "OPEN")
                    .order_by(PaperPosition.id)
                )
            ]
        return paper, [p.symbol for p in self.replay.ledger.portfolio.positions()]


def test_shared_context_order_and_all_input_permutations(
    database, settings, parity_input
):
    pair = Pair(database, settings, parity_input)
    original = deepcopy(parity_input)
    for candidates in permutations(parity_input["candidates"]):
        rows = pair.seed(candidates, NOW)
        untouched = deepcopy(rows)
        with database.session() as s:
            paper = pair.paper.contexts(s, NOW)
        replay = [c for c, _ in pair.replay.contexts(rows, set(), ms(NOW))]
        expected = ["BBBUSDT", "CCCUSDT", "AAAUSDT"]
        assert [c.symbol for c in paper] == [c.symbol for c in replay] == expected
        assert [candidate_order_key(c) for c in paper] == [
            candidate_order_key(c) for c in replay
        ]
        assert sorted(reversed(paper), key=candidate_order_key) == paper
        assert rows == untouched
    assert parity_input == original
    assert CANDIDATE_ORDER_POLICY == "SCORE_DESC_SYMBOL_ASC_V1"


def test_tied_scores_use_symbol_for_both_engines(database, settings, parity_input):
    parity_input["candidates"] = [
        dict(symbol=s, score=80, action="BUY")
        for s in ("BBBUSDT", "AAAUSDT", "CCCUSDT")
    ]
    pair = Pair(database, settings, parity_input)
    rows = pair.seed(parity_input["candidates"], NOW)
    with database.session() as s:
        paper = pair.paper.contexts(s, NOW)
    replay = pair.replay.contexts(rows, set(), ms(NOW))
    assert (
        [c.symbol for c in paper]
        == [c.symbol for c, _ in replay]
        == ["AAAUSDT", "BBBUSDT", "CCCUSDT"]
    )


@pytest.mark.parametrize(
    "capacity,expected", [(1, ["BBBUSDT"]), (2, ["BBBUSDT", "CCCUSDT"])]
)
def test_real_engines_choose_same_capacity_winners(
    database, settings, parity_input, capacity, expected
):
    parity_input["max_open_positions"] = capacity
    pair = Pair(database, settings, parity_input)
    pair.batch(parity_input["candidates"])
    assert pair.symbols() == (expected, expected)
    with database.session() as s:
        signals = list(s.scalars(select(SignalRecord).order_by(SignalRecord.id)))
        assert (
            [s.symbol for s in signals]
            == [s.symbol for s, _ in pair.last_pending]
            == ["BBBUSDT", "CCCUSDT", "AAAUSDT"]
        )


@pytest.mark.parametrize("failure", ["risk", "missing"])
def test_rejected_or_unfilled_first_candidate_does_not_stop_competition(
    database, settings, parity_input, failure
):
    pair = Pair(database, settings, parity_input)
    pair.batch(
        parity_input["candidates"],
        missing="BBBUSDT" if failure == "missing" else None,
        references={"BBBUSDT": 97} if failure == "risk" else None,
    )
    assert pair.symbols() == (["CCCUSDT"], ["CCCUSDT"])
    with database.session() as s:
        first = s.scalar(select(PaperOrder).order_by(PaperOrder.id))
        assert first.symbol == "BBBUSDT" and first.status == "REJECTED"
    if failure == "missing":
        assert any(
            e["event"] == "NO_FILL" and e["symbol"] == "BBBUSDT"
            for e in pair.replay.events
        )
        assert not any(o["symbol"] == "BBBUSDT" for o in pair.replay.execution.orders)
    else:
        assert pair.replay.blocked_reasons["invalid stop distance"] == 1


def test_existing_hold_position_preserves_remaining_candidate_order(
    database, settings, parity_input
):
    parity_input["max_open_positions"] = 2
    parity_input["existing_positions"] = ["ZZZUSDT"]
    pair = Pair(database, settings, parity_input)
    pair.batch([dict(symbol="ZZZUSDT", score=95, action="BUY")])
    pair.batch(
        parity_input["candidates"] + [dict(symbol="ZZZUSDT", score=95, action="HOLD")],
        NOW + timedelta(minutes=1),
    )
    expected = ["ZZZUSDT", "BBBUSDT"]
    assert pair.symbols() == (expected, expected)


@pytest.mark.parametrize("exit_kind", ["STRATEGY", "STOP", "TAKE_PROFIT"])
def test_required_exits_release_capacity_before_buys(
    database, settings, parity_input, exit_kind
):
    pair = Pair(database, settings, parity_input)
    pair.batch([dict(symbol="ZZZUSDT", score=95, action="BUY")])
    later = NOW + timedelta(minutes=1)
    candidates = parity_input["candidates"] + [
        dict(symbol="ZZZUSDT", score=30, action="SELL")
    ]
    if exit_kind != "STRATEGY":
        # Replay protection is a separate minute layer; never conflate it with signals.
        price = 97 if exit_kind == "STOP" else 104
        if exit_kind == "TAKE_PROFIT":
            with database.session.begin() as s:
                s.scalar(select(PaperPosition)).take_profit_price = D(103)
            pair.replay.ledger.portfolio.positions()[0].take_profit_price = D(103)
        rows = pair.seed(candidates, later)
        for r in rows:
            pair.quotes.set(
                price if r["symbol"] == "ZZZUSDT" else 100,
                when=later,
                symbol=r["symbol"],
            )
        pair.paper.run_once(later)
        # Check protection-first via the production ledger; replay's unchanged
        # minute STOP/TP path has its own maintained regression coverage.
        with database.session() as s:
            orders = list(s.scalars(select(PaperOrder).order_by(PaperOrder.id)))
            assert [(o.symbol, o.side) for o in orders if o.status == "FILLED"][:3] == [
                ("ZZZUSDT", "BUY"),
                ("ZZZUSDT", "SELL"),
                ("BBBUSDT", "BUY"),
            ]
        return
    pair.batch(candidates, later)
    assert pair.symbols() == (["BBBUSDT"], ["BBBUSDT"])
    assert [(o["symbol"], o["side"]) for o in pair.replay.execution.orders] == [
        ("ZZZUSDT", "BUY"),
        ("ZZZUSDT", "SELL"),
        ("BBBUSDT", "BUY"),
    ]


def test_default_replay_context_order_matches_pre_fix_contract(
    database, settings, parity_input
):
    pair = Pair(database, settings, parity_input)
    rows = pair.seed(parity_input["candidates"], NOW)
    contexts = pair.replay.contexts(rows, set(), ms(NOW))
    assert contexts == sorted(
        contexts, key=lambda item: (-item[0].score, item[0].symbol)
    )
