import json
import logging
from datetime import datetime, timezone


class JsonFormatter(logging.Formatter):
    def format(self, record):
        data = dict(
            timestamp=datetime.now(timezone.utc).isoformat(),
            level=record.levelname,
            event=record.getMessage(),
            **getattr(record, "event_data", {}),
        )
        if record.exc_info:
            data["exception"] = self.formatException(record.exc_info)
        return json.dumps(data, ensure_ascii=False)


def configure_logging():
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
