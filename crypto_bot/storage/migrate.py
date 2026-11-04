from pathlib import Path

from alembic import command
from alembic.config import Config


def migration_config(connection):
    config = Config()
    config.set_main_option(
        "script_location", str(Path(__file__).with_name("migrations"))
    )
    config.attributes["connection"] = connection
    return config


def upgrade_database(database):
    # SQLite nullable-column batch rebuild must preserve referencing ledger rows.
    # FK enforcement is disabled only on this migration connection, outside BEGIN;
    # integrity is checked before commit and enforcement restored afterwards.
    with database.engine.connect() as connection:
        sqlite = connection.dialect.name == "sqlite"
        if sqlite:
            connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
            connection.commit()
        try:
            with connection.begin():
                command.upgrade(migration_config(connection), "head")
                if (
                    sqlite
                    and connection.exec_driver_sql(
                        "PRAGMA foreign_key_check"
                    ).fetchone()
                ):
                    raise RuntimeError("Migration foreign-key integrity check failed")
        finally:
            if sqlite:
                connection.exec_driver_sql("PRAGMA foreign_keys=ON")
                connection.commit()
