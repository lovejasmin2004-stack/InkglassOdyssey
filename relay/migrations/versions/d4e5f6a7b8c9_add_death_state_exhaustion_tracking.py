"""add death_state_exhaustion_gained to characters

Revision ID: d4e5f6a7b8c9
Revises: c8f2a31d9e74
Create Date: 2026-05-11 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d4e5f6a7b8c9"
down_revision: str | Sequence[str] | None = "c8f2a31d9e74"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add death_state_exhaustion_gained column to characters (#6).

    Tracks how many exhaustion points have been gained from the current
    death state, capped at 3 per the design spec.
    """
    op.add_column(
        "characters",
        sa.Column("death_state_exhaustion_gained", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    """Remove death state exhaustion tracking column."""
    op.drop_column("characters", "death_state_exhaustion_gained")
