"""Separate read-only SQLite connection pool; never initializes or migrates DBs."""

from pathlib import Path
from urllib.parse import quote

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


class ReadDatabase:
    def __init__(self, source):
        url = source.engine.url
        self.owned = False
        if url.get_backend_name() == "sqlite" and url.database not in (
            None,
            ":memory:",
        ):
            path = quote(str(Path(url.database).resolve()), safe="/")
            self.engine = create_engine(
                f"sqlite:///file:{path}?mode=ro&uri=true",
                connect_args={"timeout": 2, "check_same_thread": False},
            )
            self.session = sessionmaker(
                self.engine, expire_on_commit=False, autoflush=False
            )
            self.owned = True
        else:
            # In-memory deterministic fixtures have no separate file to open.
            self.engine = source.engine
            self.session = source.session

    def close(self):
        if self.owned:
            self.engine.dispose()
