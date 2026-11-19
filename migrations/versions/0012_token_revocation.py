"""token revocation list

Revision ID: 0012_token_revocation
Revises: 0011_dataset_freeze
Create Date: 2026-11-06 10:00:00.000000

`specs/target-architecture` T12：支持按 jti 精确撤销与按 subject 批量撤销，
使"禁用身份/降权"具备可测量的生效时间（AC-3.3 / A07）。

建表可重复执行；downgrade 直接删表，撤销记录属运行期数据，不需要回填。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0012_token_revocation"
down_revision = "0011_dataset_freeze"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "token_revocation" in inspector.get_table_names():
        return
    op.create_table(
        "token_revocation",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("value", sa.String(length=256), nullable=False),
        sa.Column("reason", sa.String(length=256), nullable=True),
        sa.Column("revoked_before", sa.DateTime(), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("created_by", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("kind", "value", name="uk_token_revocation"),
    )
    op.create_index(
        "idx_token_revocation_kind_value", "token_revocation", ["kind", "value"]
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "token_revocation" not in inspector.get_table_names():
        return
    index_names = {index["name"] for index in inspector.get_indexes("token_revocation")}
    if "idx_token_revocation_kind_value" in index_names:
        op.drop_index("idx_token_revocation_kind_value", table_name="token_revocation")
    op.drop_table("token_revocation")
