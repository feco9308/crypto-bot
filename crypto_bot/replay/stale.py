"""Replay-only stale metadata and conservative risk input; no order sender."""

from dataclasses import dataclass, fields
from decimal import Decimal

from crypto_bot.replay.clock import at, ms
from crypto_bot.trading.domain import ZERO, D, PortfolioView, q

STRICT = "STRICT_FRESH_MARKS"
QUARANTINE = "RESEARCH_QUARANTINE_STALE"
POLICIES = (STRICT, QUARANTINE)
WARNING = (
    "RESULT CONTAINS STALE OPEN POSITIONS. "
    "Final equity contains stale valuations and must not be interpreted as a fully "
    "liquidatable portfolio value."
)


def state(position):
    return position.entry_data.setdefault(
        "stale_state",
        dict(
            stale=False,
            quarantined=False,
            first_stale_time=None,
            stale_since=None,
            # Old checkpoints have no mark observation timestamp. Do not invent
            # one from opened_at; a fresh replay mark will set it explicitly.
            last_valid_mark_time=None,
            last_valid_mark_price=str(position.current_price),
            stale_duration_seconds=0,
            resumed_after_stale=False,
        ),
    )


def duration(value, now):
    return value["stale_duration_seconds"] + (
        (now - ms(value["stale_since"])) / 1000 if value["stale"] else 0
    )


def update(position, fresh, now, event, policy):
    value = state(position)
    stamp = at(now).isoformat() + "Z"
    if not fresh and not value["stale"]:
        value["stale"] = True
        value["first_stale_time"] = value["first_stale_time"] or stamp
        value["stale_since"] = stamp
        event(
            "POSITION_STALE",
            now,
            position_id=position.id,
            symbol=position.symbol,
            stale_position_policy=policy,
            last_valid_mark_time=value["last_valid_mark_time"],
            last_valid_mark_price=value["last_valid_mark_price"],
        )
    elif fresh and value["stale"]:
        value["stale_duration_seconds"] = duration(value, now)
        value["stale"] = False
        value["stale_since"] = None
        value["resumed_after_stale"] = True
        event(
            "POSITION_RESUMED",
            now,
            position_id=position.id,
            symbol=position.symbol,
            stale_duration_seconds=value["stale_duration_seconds"],
        )


def position_metadata(position, now):
    value = state(position)
    cost = position.notional + position.entry_fee
    market = q(position.quantity * position.current_price)
    return dict(
        position_id=position.id,
        symbol=position.symbol,
        stale=value["stale"],
        quarantined=value.get("quarantined", False),
        first_stale_time=value["first_stale_time"],
        last_valid_mark_time=value["last_valid_mark_time"],
        last_valid_mark_price=value["last_valid_mark_price"],
        stale_duration_seconds=duration(value, now),
        resumed_after_stale=value["resumed_after_stale"],
        capital_locked_by_stale_positions=str(
            cost if value["stale"] and position.status == "OPEN" else ZERO
        ),
        exposure_locked_by_stale_positions=str(
            max(market, cost) if value["stale"] and position.status == "OPEN" else ZERO
        ),
    )


@dataclass(frozen=True)
class ReplayRiskView(PortfolioView):
    stale_exposure_reservation: Decimal = ZERO

    @property
    def exposure(self):
        return super().exposure + self.stale_exposure_reservation


def risk_view(portfolio):
    view = portfolio.view()
    withheld, locked = ZERO, ZERO
    for position in portfolio.positions():
        if state(position)["stale"]:
            market = q(position.quantity * position.current_price)
            cost = position.notional + position.entry_fee
            withheld += max(ZERO, market - cost)
            locked += max(ZERO, cost - market)
    values = {f.name: getattr(view, f.name) for f in fields(view)}
    values.update(
        equity=q(view.equity - withheld), daily_pnl=q(view.daily_pnl - withheld)
    )
    return ReplayRiskView(**values, stale_exposure_reservation=locked)


def summary(trades, policy):
    episodes = [t for t in trades if t.get("first_stale_time")]
    stale = [t for t in trades if t["status"] == "OPEN_STALE"]
    return dict(
        stale_position_policy=policy,
        stale_position_count=len(stale),
        stale_symbols=sorted(t["symbol"] for t in stale),
        stale_positions=[
            {
                k: t.get(k)
                for k in (
                    "position_id",
                    "symbol",
                    "status",
                    "first_stale_time",
                    "last_valid_mark_time",
                    "last_valid_mark_price",
                    "stale_duration_seconds",
                    "resumed_after_stale",
                    "capital_locked_by_stale_positions",
                    "exposure_locked_by_stale_positions",
                )
            }
            for t in stale
        ],
        first_stale_time=min((t["first_stale_time"] for t in episodes), default=None),
        stale_duration_seconds=sum(
            t.get("stale_duration_seconds", 0) for t in episodes
        ),
        resumed_after_stale=any(t.get("resumed_after_stale") for t in episodes),
        capital_locked_by_stale_positions=str(
            sum((D(t["capital_locked_by_stale_positions"]) for t in stale), ZERO)
        ),
        exposure_locked_by_stale_positions=str(
            sum((D(t["exposure_locked_by_stale_positions"]) for t in stale), ZERO)
        ),
        stale_data_affected=bool(episodes),
        ranking_eligible=not bool(episodes),
        valuation_complete=not bool(stale),
        metrics_complete=not bool(episodes),
        stale_warning=WARNING if stale else None,
        valuation_model="CASH_PLUS_FRESH_MARKS_PLUS_LAST_KNOWN_STALE_MARKS",
    )
