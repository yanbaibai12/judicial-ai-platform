"""baseline schema

Revision ID: 0001_baseline
Revises:
Create Date: 2026-10-28
"""

from __future__ import annotations

from alembic import op

from judicial_ai.db import Base

revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    from judicial_ai import models  # noqa: F401

    Base.metadata.create_all(bind=op.get_bind())


def downgrade() -> None:
    # Baseline downgrade is intentionally a no-op. Dropping every production table
    # from a baseline is more dangerous than leaving the database intact.
    pass
