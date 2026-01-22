"""add regions and retention policy

Revision ID: a9fee243e6de
Revises: fa270608b668
Create Date: 2026-01-22 19:06:42.565563

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'a9fee243e6de'
down_revision: Union[str, Sequence[str], None] = 'fa270608b668'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('sites', sa.Column('regions', postgresql.ARRAY(sa.String()), server_default=sa.text("'{\"global\"}'"), nullable=False))
    
    op.execute("SELECT add_retention_policy('metrics', INTERVAL '30 days');")
    
    


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("SELECT remove_retention_policy('metrics');")
    op.drop_column('sites', 'regions')
