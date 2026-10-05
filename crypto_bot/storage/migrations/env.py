from alembic import context

from crypto_bot.storage.models import Base
from crypto_bot.storage.paper_models import ExtensionBase

config = context.config
connection = config.attributes.get("connection")
if connection is None:
    raise RuntimeError("Use market-scanner migrate (configured connection required)")
context.configure(
    connection=connection,
    target_metadata=[Base.metadata, ExtensionBase.metadata],
    compare_type=True,
)
with context.begin_transaction():
    context.run_migrations()
