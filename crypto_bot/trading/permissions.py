"""Entry permissions are separate from scanner observation overrides."""

from sqlalchemy import select

from crypto_bot.storage.models import Decision, Instrument, Override, ScannerRun
from crypto_bot.storage.paper_models import PaperAccount, SymbolTradePermission


def entry_permission(session, symbol, now, max_age):
    instrument = session.get(Instrument, symbol)
    override = session.get(Override, symbol)
    state = override.status if override else "AUTO"
    flag = session.get(SymbolTradePermission, symbol)
    manual = bool(flag and flag.manual_trade_enabled)
    run = session.scalar(
        select(ScannerRun)
        .where(
            ScannerRun.timeframe == "1h", ScannerRun.status.in_(["complete", "partial"])
        )
        .order_by(ScannerRun.id.desc())
        .limit(1)
    )
    decision = (
        session.scalar(
            select(Decision).where(Decision.run_id == run.id, Decision.symbol == symbol)
        )
        if run and 0 <= (now - run.timestamp).total_seconds() <= max_age
        else None
    )
    auto = bool(decision and decision.algorithm_watch)
    account = session.get(PaperAccount, 1)
    eligible = bool(
        instrument
        and account
        and instrument.quote_asset == account.quote_asset
        and instrument.active
        and instrument.spot
        and state != "IGNORE"
        and (auto or manual)
    )
    return dict(
        algorithm_watch=auto,
        manual_trade_enabled=manual,
        watch_override=state,
        entry_eligible=eligible,
    )
