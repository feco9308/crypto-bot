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
    with database.engine.begin() as connection:
        command.upgrade(migration_config(connection), "head")
