"""add region to metrics

Revision ID: fa270608b668
Revises: 1b6cfdaf0b2e
Create Date: 2026-01-17 13:19:46.268357

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'fa270608b668'
down_revision: Union[str, Sequence[str], None] = '1b6cfdaf0b2e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('metrics', sa.Column('region', sa.String(length=50), server_default='global', nullable=False))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('metrics', 'region')
