"""add updated_at to scenes

Revision ID: a3d1f7e92b10
Revises: 7bc64702df81
Create Date: 2026-05-10 12:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'a3d1f7e92b10'
down_revision: str | Sequence[str] | None = '7bc64702df81'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add updated_at column to scenes table."""
    op.add_column('scenes', sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()))


def downgrade() -> None:
    """Remove updated_at column from scenes table."""
    op.drop_column('scenes', 'updated_at')
