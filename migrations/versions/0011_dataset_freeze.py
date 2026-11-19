"""dataset version freeze markers

Revision ID: 0011_dataset_freeze
Revises: 0010_credential_hash_algo
Create Date: 2026-11-05 10:00:00.000000

`biz-quality-eval` AC-1.1/AC-1.2：冻结版本必须带内容哈希与脱敏规则版本，
并且冻结后拒绝写入。加列可空、可重复执行，出问题直接降级。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0011_dataset_freeze"
down_revision = "0010_credential_hash_algo"
branch_labels = None
depends_on = None

_COLUMNS = (
    ("content_hash", sa.Column("content_hash", sa.String(length=64), nullable=True)),
    (
        "redaction_rule_version",
        sa.Column("redaction_rule_version", sa.String(length=64), nullable=True),
    ),
    ("frozen_at", sa.Column("frozen_at", sa.DateTime(), nullable=True)),
)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("dataset_version")}
    for name, column in _COLUMNS:
        if name not in columns:
            op.add_column("dataset_version", column)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("dataset_version")}
    for name, _column in _COLUMNS:
        if name in columns:
            op.drop_column("dataset_version", name)