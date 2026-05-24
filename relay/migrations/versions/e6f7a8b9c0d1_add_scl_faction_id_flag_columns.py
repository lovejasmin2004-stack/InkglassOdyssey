"""add faction_id and flag columns to state_change_log

Revision ID: e6f7a8b9c0d1
Revises: d5e6f7a8b9c0
Create Date: 2026-05-22 14:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e6f7a8b9c0d1"
down_revision: str | Sequence[str] | None = "d5e6f7a8b9c0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add faction_id and flag columns to state_change_log for world mutations."""
    with op.batch_alter_table("state_change_log", schema=None) as batch_op:
        batch_op.add_column(sa.Column("faction_id", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("flag", sa.String(), nullable=True))


def downgrade() -> None:
    """Remove faction_id and flag columns from state_change_log."""
    with op.batch_alter_table("state_change_log", schema=None) as batch_op:
        batch_op.drop_column("flag")
        batch_op.drop_column("faction_id")
