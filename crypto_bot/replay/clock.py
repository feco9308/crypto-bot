from dataclasses import dataclass
from datetime import datetime, timezone


def utc(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return (
        value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo else value
    )


def ms(value):
    return int(utc(value).replace(tzinfo=timezone.utc).timestamp() * 1000)


def at(value):
    return datetime.fromtimestamp(value / 1000, timezone.utc).replace(tzinfo=None)


@dataclass
class ReplayClock:
    now_ms: int

    def advance(self, timestamp):
        if timestamp < self.now_ms:
            raise ValueError("Replay clock cannot move backwards")
        self.now_ms = timestamp

    def closed(self, candles):
        return [c for c in candles if c.close_time < self.now_ms]
