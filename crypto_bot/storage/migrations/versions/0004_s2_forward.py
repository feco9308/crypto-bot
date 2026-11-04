"""Nullable paper stops and auditable forward generations/daily idempotency."""

import sqlalchemy as sa
from alembic import op

revision = "0004_s2_forward"
down_revision = "0003_quotes_permissions"
branch_labels = None
depends_on = None


def upgrade():
    money = sa.Numeric(30, 12).with_variant(sa.Text(), "sqlite")
    with op.batch_alter_table("paper_positions") as batch:
        batch.alter_column("stop_price", existing_type=money, nullable=True)
    op.create_table(
        "paper_generations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("activated_at", sa.DateTime(), nullable=False),
        sa.Column("strategy_name", sa.String(80), nullable=False),
        sa.Column("execution_profile", sa.String(50), nullable=False),
        sa.Column("initial_balance", money, nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("account_before", sa.JSON(), nullable=False),
        sa.Column("history_cutoffs", sa.JSON(), nullable=False),
        sa.Column("provenance", sa.JSON(), nullable=False),
        sa.Column("latest_indicators", sa.JSON(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
    )
    op.create_table(
        "paper_forward_daily_decisions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "generation_id",
            sa.Integer(),
            sa.ForeignKey("paper_generations.id"),
            nullable=False,
        ),
        sa.Column("strategy_name", sa.String(80), nullable=False),
        sa.Column("symbol", sa.String(40), nullable=False),
        sa.Column("source_open_ms", sa.BigInteger(), nullable=False),
        sa.Column("source_close_ms", sa.BigInteger(), nullable=False),
        sa.Column("decision_time", sa.DateTime(), nullable=False),
        sa.Column("state_before", sa.String(8), nullable=False),
        sa.Column("state_after", sa.String(8), nullable=False),
        sa.Column("action", sa.String(8), nullable=False),
        sa.Column("indicators", sa.JSON(), nullable=False),
        sa.Column(
            "signal_id",
            sa.Integer(),
            sa.ForeignKey("strategy_signals.id"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "order_id",
            sa.Integer(),
            sa.ForeignKey("paper_orders.id"),
            nullable=True,
            unique=True,
        ),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("execution_audit", sa.JSON(), nullable=True),
        sa.UniqueConstraint(
            "generation_id",
            "strategy_name",
            "symbol",
            "source_close_ms",
            name="uq_forward_daily_generation_candle",
        ),
    )


def downgrade():
    raise RuntimeError(
        "Forward ledger history is retained; destructive downgrade unsupported"
    )
