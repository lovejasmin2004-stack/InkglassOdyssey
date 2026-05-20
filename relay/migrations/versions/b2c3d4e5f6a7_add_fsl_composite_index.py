"""add composite index to faction_standing_log

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-05-20 14:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b2c3d4e5f6a7"
down_revision: str | Sequence[str] | None = "a1b2c3d4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add composite index for efficient log queries by character + faction."""
    with op.batch_alter_table("faction_standing_log", schema=None) as batch_op:
        batch_op.create_index(
            "ix_fsl_char_faction_created",
            ["character_id", "faction_id", "created_at"],
            unique=False,
        )


def downgrade() -> None:
    """Remove composite index."""
    with op.batch_alter_table("faction_standing_log", schema=None) as batch_op:
        batch_op.drop_index("ix_fsl_char_faction_created")
