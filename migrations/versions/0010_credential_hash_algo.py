"""credential hash algorithm marker

Revision ID: 0010_credential_hash_algo
Revises: 0009_enterprise_controls
Create Date: 2026-11-04 10:00:00.000000

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0010_credential_hash_algo"
down_revision = "0009_enterprise_controls"
branch_labels = None
depends_on = None

HMAC_PREFIX = "hmac-sha256$"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("app_credential")}
    if "hash_algo" not in columns:
        op.add_column("app_credential", sa.Column("hash_algo", sa.String(length=16), nullable=True))

    op.execute(
        sa.text(
            "UPDATE app_credential SET hash_algo = :hmac "
            "WHERE key_hash LIKE :prefix AND (hash_algo IS NULL OR hash_algo = '')"
        ).bindparams(hmac="hmac-sha256", prefix=f"{HMAC_PREFIX}%")
    )
    op.execute(
        sa.text(
            "UPDATE app_credential SET hash_algo = :legacy "
            "WHERE hash_algo IS NULL OR hash_algo = ''"
        ).bindparams(legacy="sha256")
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("app_credential")}
    if "hash_algo" in columns:
        op.drop_column("app_credential", "hash_algo")