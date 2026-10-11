"""Idle replay-worker shadow queue, under its existing singleton lock."""

from crypto_bot.replay.models import now
from crypto_bot.replay.shadow_engine import ShadowEngine
from crypto_bot.replay.shadow_store import ShadowStore


def once(replay_worker):
    store = ShadowStore(replay_worker.store.path.parent / "shadow-exit.db")
    ident = store.claim()
    if ident is None:
        return False
    replay_worker.active = "shadow:" + ident

    def check():
        # Shadow has no access to normal replay controls or anchor account state.
        if replay_worker.stop.is_set():
            from crypto_bot.replay.worker import Stopping

            raise Stopping()

    try:
        provider = replay_worker.provider_factory(replay_worker.cache, check=check)
        engine = ShadowEngine(
            provider, check, lambda **v: store.update(ident, progress=v)
        )
        result = engine.run(store.get(ident, include_source=True))
        store.update(ident, status="COMPLETED", result=result, finished_at=now())
    except Exception as exc:
        from crypto_bot.replay.worker import Stopping

        if isinstance(exc, Stopping):
            store.update(ident, status="QUEUED")
        else:
            store.update(
                ident,
                status="FAILED",
                error=f"{type(exc).__name__}: {str(exc)[:240]}",
                finished_at=now(),
            )
            raise
    finally:
        replay_worker.active = None
    return True
