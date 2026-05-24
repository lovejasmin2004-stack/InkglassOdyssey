"""add character_preferences table

Revision ID: b9c0d1e2f3a4
Revises: a8b9c0d1e2f3
Create Date: 2026-05-23 10:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b9c0d1e2f3a4"
down_revision: str | None = "a8b9c0d1e2f3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "character_preferences",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("character_id", sa.String(), nullable=False),
        sa.Column("world_id", sa.String(), nullable=False),
        sa.Column("backstory_blurb", sa.Text(), nullable=False, server_default=""),
        sa.Column("story_interests", sa.JSON(), nullable=False),
        sa.Column("topics_to_avoid", sa.JSON(), nullable=False),
        sa.Column("content_rating", sa.String(), nullable=False, server_default="moderate"),
        sa.Column("narrative_pace", sa.String(), nullable=False, server_default="moderate"),
        sa.Column("companion_interest", sa.String(), nullable=False, server_default="moderate"),
        sa.Column("exploration_style", sa.String(), nullable=False, server_default="balanced"),
        sa.Column("npc_notes", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["character_id"], ["characters.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_character_preferences_character_id",
        "character_preferences",
        ["character_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_character_preferences_character_id", table_name="character_preferences")
    op.drop_table("character_preferences")
