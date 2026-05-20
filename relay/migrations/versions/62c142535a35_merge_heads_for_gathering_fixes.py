"""merge heads for gathering fixes

Revision ID: 62c142535a35
Revises: d4e5f6a7b8c9, f2a3b4c5d6e7
Create Date: 2026-05-19 07:33:58.055500

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '62c142535a35'
down_revision: Union[str, Sequence[str], None] = ('d4e5f6a7b8c9', 'f2a3b4c5d6e7')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
