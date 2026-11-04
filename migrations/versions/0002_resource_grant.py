"""resource grant (fine-grained ACL)

Revision ID: 0002_resource_grant
Revises: a48e5fe01609
Create Date: 2026-10-29

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002_resource_grant"
down_revision = "a48e5fe01609"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "resource_grant" in inspector.get_table_names():
        return
    op.create_table(
        "resource_grant",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("tenant_id", sa.BigInteger(), nullable=False, index=True),
        sa.Column("resource_type", sa.String(length=32), nullable=False),
        sa.Column("resource_id", sa.BigInteger(), nullable=False, index=True),
        sa.Column("subject_kind", sa.String(length=16), nullable=False),
        sa.Column("subject_id", sa.String(length=128), nullable=False),
        sa.Column("level", sa.String(length=16), nullable=False, server_default="read"),
        sa.Column("granted_by", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="ACTIVE"),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint(
            "resource_type",
            "resource_id",
            "subject_kind",
            "subject_id",
            name="uk_resource_grant_subject",
        ),
    )
    op.create_index(
        "idx_resource_grant_subject", "resource_grant", ["subject_kind", "subject_id"]
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "resource_grant" not in inspector.get_table_names():
        return
    index_names = {index["name"] for index in inspector.get_indexes("resource_grant")}
    if "idx_resource_grant_subject" in index_names:
        op.drop_index("idx_resource_grant_subject", table_name="resource_grant")
    op.drop_table("resource_grant")
