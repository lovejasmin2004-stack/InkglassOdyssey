"""add retry tracking to pending_turns

Revision ID: c8f2a31d9e74
Revises: a3d1f7e92b10
Create Date: 2026-05-10 21:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c8f2a31d9e74"
down_revision: str | Sequence[str] | None = "a3d1f7e92b10"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add retry_count and parent_turn_id columns to pending_turns (#4)."""
    op.add_column(
        "pending_turns",
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "pending_turns",
        sa.Column("parent_turn_id", sa.String(), nullable=True),
    )


def downgrade() -> None:
    """Remove retry tracking columns."""
    op.drop_column("pending_turns", "parent_turn_id")
    op.drop_column("pending_turns", "retry_count")
