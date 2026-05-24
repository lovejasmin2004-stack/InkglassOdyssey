"""add narrative_threads table

Revision ID: f7a8b9c0d1e2
Revises: e6f7a8b9c0d1
Create Date: 2026-05-22 16:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f7a8b9c0d1e2"
down_revision: str | Sequence[str] | None = "e6f7a8b9c0d1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the narrative_threads table (§6 Three-Layer Narrative)."""
    op.create_table(
        "narrative_threads",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("character_id", sa.String(), nullable=False),
        sa.Column("world_id", sa.String(), nullable=False),
        sa.Column("thread_key", sa.String(), nullable=False),
        sa.Column("signal_type", sa.String(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("related_npcs", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("related_regions", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("mention_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("status", sa.String(), nullable=False, server_default="'active'"),
        sa.Column("first_seen_session_id", sa.String(), nullable=True),
        sa.Column("last_seen_session_id", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["character_id"], ["characters.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("character_id", "thread_key", name="uq_nt_char_thread"),
    )
    op.create_index("ix_nt_character_status", "narrative_threads", ["character_id", "status"])


def downgrade() -> None:
    """Drop the narrative_threads table."""
    op.drop_index("ix_nt_character_status", table_name="narrative_threads")
    op.drop_table("narrative_threads")
