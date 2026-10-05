"""Current quote audit and independent manual entry permissions; scanner untouched."""

import sqlalchemy as sa
from alembic import op

revision = "0003_quotes_permissions"
down_revision = "0002_paper"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "paper_symbol_permissions",
        sa.Column("symbol", sa.String(40), nullable=False),
        sa.Column(
            "manual_trade_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["symbol"], ["instruments.symbol"]),
        sa.PrimaryKeyConstraint("symbol"),
    )
    op.add_column(
        "risk_decisions", sa.Column("execution_quote", sa.JSON(), nullable=True)
    )
    op.add_column("paper_orders", sa.Column("quote", sa.JSON(), nullable=True))
    op.add_column("paper_positions", sa.Column("mark_quote", sa.JSON(), nullable=True))
    op.add_column(
        "paper_positions", sa.Column("marked_at", sa.DateTime(), nullable=True)
    )
    op.add_column(
        "paper_portfolio_snapshots", sa.Column("quote_status", sa.JSON(), nullable=True)
    )


def downgrade():
    op.drop_column("paper_portfolio_snapshots", "quote_status")
    op.drop_column("paper_positions", "marked_at")
    op.drop_column("paper_positions", "mark_quote")
    op.drop_column("paper_orders", "quote")
    op.drop_column("risk_decisions", "execution_quote")
    op.drop_table("paper_symbol_permissions")
