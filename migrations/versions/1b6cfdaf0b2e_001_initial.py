"""001_initial

Revision ID: 1b6cfdaf0b2e
Revises:
Create Date: 2026-01-09 12:10:23.740215

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "1b6cfdaf0b2e"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS timescaledb;")

    op.create_table(
        "sites",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False
        ),
        sa.Column("url", sa.String(length=2048), nullable=False),
        sa.Column(
            "interval", sa.Integer(), nullable=False, server_default=sa.text("60")
        ),
        sa.Column(
            "is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    op.create_table(
        "metrics",
        sa.Column("time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("site_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=False),
        sa.Column("latency_ms", sa.Float(), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["site_id"], ["sites.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("time", "site_id", name="pk_metrics"),
    )

    op.create_index("ix_metrics_site_time_desc", "metrics", ["site_id", "time"])

    op.execute("SELECT create_hypertable('metrics', 'time', if_not_exists => TRUE);")


def downgrade() -> None:
    op.drop_index("ix_metrics_site_time_desc", table_name="metrics")
    op.drop_table("metrics")
    op.drop_table("sites")
