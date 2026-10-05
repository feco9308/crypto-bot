import math
import os
from dataclasses import dataclass

from dotenv import load_dotenv


@dataclass(frozen=True)
class MonitorSettings:
    heartbeat_interval: float = 30
    stale_threshold: float = 120
    event_retention_count: int = 500

    def __post_init__(self):
        if (
            not math.isfinite(self.heartbeat_interval)
            or self.heartbeat_interval <= 0
            or not math.isfinite(self.stale_threshold)
            or self.stale_threshold <= 2 * self.heartbeat_interval
            or self.event_retention_count < 1
        ):
            raise ValueError("Invalid monitoring interval/retention")

    @classmethod
    def load(cls):
        load_dotenv()
        return cls(
            float(os.getenv("MONITOR_HEARTBEAT_INTERVAL", "30")),
            float(os.getenv("MONITOR_STALE_THRESHOLD", "120")),
            int(os.getenv("MONITOR_EVENT_RETENTION_COUNT", "500")),
        )
