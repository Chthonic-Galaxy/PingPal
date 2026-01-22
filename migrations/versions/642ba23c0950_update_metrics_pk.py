"""update metrics pk

Revision ID: 642ba23c0950
Revises: a9fee243e6de
Create Date: 2026-01-22 19:34:21.238892

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '642ba23c0950'
down_revision: Union[str, Sequence[str], None] = 'a9fee243e6de'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_constraint('pk_metrics', 'metrics', type_='primary')
    op.create_primary_key('pk_metrics', 'metrics', ['time', 'site_id', 'region'])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint('pk_metrics', 'metrics', type_='primary')
    op.create_primary_key('pk_metrics', 'metrics', ['time', 'site_id'])
