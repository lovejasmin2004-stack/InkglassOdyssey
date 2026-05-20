"""add faction_standing_log table

Revision ID: a1b2c3d4e5f6
Revises: f150ff510bac
Create Date: 2026-05-20 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4e5f6"
down_revision: str | Sequence[str] | None = "f150ff510bac"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "faction_standing_log",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("player_id", sa.String(), nullable=False),
        sa.Column("character_id", sa.String(), nullable=False),
        sa.Column("world_id", sa.String(), nullable=False),
        sa.Column("faction_id", sa.String(), nullable=False),
        sa.Column("old_standing", sa.Integer(), nullable=False),
        sa.Column("new_standing", sa.Integer(), nullable=False),
        sa.Column("delta", sa.Integer(), nullable=False),
        sa.Column("old_tier", sa.String(), nullable=False),
        sa.Column("new_tier", sa.String(), nullable=False),
        sa.Column("tier_changed", sa.Boolean(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("source_faction_id", sa.String(), nullable=True),
        sa.Column("reason", sa.String(), nullable=False, server_default=""),
        sa.Column("session_id", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["character_id"],
            ["characters.id"],
        ),
        sa.ForeignKeyConstraint(
            ["player_id"],
            ["accounts.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("faction_standing_log", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_faction_standing_log_player_id"), ["player_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_faction_standing_log_character_id"), ["character_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_faction_standing_log_faction_id"), ["faction_id"], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("faction_standing_log", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_faction_standing_log_faction_id"))
        batch_op.drop_index(batch_op.f("ix_faction_standing_log_character_id"))
        batch_op.drop_index(batch_op.f("ix_faction_standing_log_player_id"))

    op.drop_table("faction_standing_log")
