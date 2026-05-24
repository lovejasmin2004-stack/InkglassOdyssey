"""add consequence system tables (npc_instance_state, world_flags)

Revision ID: d5e6f7a8b9c0
Revises: c3d4e5f6a7b8
Create Date: 2026-05-22 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d5e6f7a8b9c0"
down_revision: str | Sequence[str] | None = "c3d4e5f6a7b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # --- NPC Instance State ---
    op.create_table(
        "npc_instance_state",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("character_id", sa.String(), nullable=False),
        sa.Column("npc_id", sa.String(), nullable=False),
        sa.Column("world_id", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="alive"),
        sa.Column("hp_current", sa.Integer(), nullable=True),
        sa.Column("disposition_override", sa.Integer(), nullable=True),
        sa.Column("flags", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("last_interaction_summary", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["character_id"],
            ["characters.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("character_id", "npc_id", name="uq_nis_char_npc"),
    )
    with op.batch_alter_table("npc_instance_state", schema=None) as batch_op:
        batch_op.create_index("ix_nis_character_id", ["character_id"], unique=False)
        batch_op.create_index("ix_nis_npc_id", ["npc_id"], unique=False)

    # --- World Flags ---
    op.create_table(
        "world_flags",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("character_id", sa.String(), nullable=False),
        sa.Column("flag", sa.String(), nullable=False),
        sa.Column("value", sa.String(), nullable=False, server_default="true"),
        sa.Column("reason", sa.String(), nullable=False, server_default=""),
        sa.Column("source", sa.String(), nullable=False, server_default="consequence"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["character_id"],
            ["characters.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("character_id", "flag", name="uq_wf_char_flag"),
    )
    with op.batch_alter_table("world_flags", schema=None) as batch_op:
        batch_op.create_index("ix_wf_character_id", ["character_id"], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("world_flags", schema=None) as batch_op:
        batch_op.drop_index("ix_wf_character_id")
    op.drop_table("world_flags")

    with op.batch_alter_table("npc_instance_state", schema=None) as batch_op:
        batch_op.drop_index("ix_nis_npc_id")
        batch_op.drop_index("ix_nis_character_id")
    op.drop_table("npc_instance_state")
