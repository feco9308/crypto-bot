"""Adopt existing v1 scanner without deleting or altering data."""

from alembic import op
from sqlalchemy import select

from crypto_bot.storage.models import Base, SchemaVersion

revision = "0001_scanner"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    Base.metadata.create_all(bind, checkfirst=True)
    version = bind.execute(
        select(SchemaVersion.version).where(SchemaVersion.id == 1)
    ).scalar()
    if version is None:
        bind.execute(SchemaVersion.__table__.insert().values(id=1, version=1))
    elif version != 1:
        raise RuntimeError("Unsupported scanner schema; refusing adoption")


def downgrade():
    raise RuntimeError("Scanner data downgrade/removal is deliberately unsupported")
