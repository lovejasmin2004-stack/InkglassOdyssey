"""merge heads for gathering fixes

Revision ID: 62c142535a35
Revises: d4e5f6a7b8c9, f2a3b4c5d6e7
Create Date: 2026-05-19 07:33:58.055500

"""

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "62c142535a35"
down_revision: str | Sequence[str] | None = ("d4e5f6a7b8c9", "f2a3b4c5d6e7")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
