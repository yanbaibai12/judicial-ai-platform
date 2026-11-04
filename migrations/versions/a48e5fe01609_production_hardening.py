"""production hardening

Revision ID: a48e5fe01609
Revises: 0001_baseline
Create Date: 2026-10-28 16:44:10.205691

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a48e5fe01609"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("audit_log")}
    index_names = {index["name"] for index in inspector.get_indexes("audit_log")}
    if "request_id" not in columns:
        op.add_column("audit_log", sa.Column("request_id", sa.String(length=64), nullable=True))
    if "trace_id" not in columns:
        op.add_column("audit_log", sa.Column("trace_id", sa.String(length=64), nullable=True))
    if "ix_audit_log_request_id" not in index_names:
        op.create_index("ix_audit_log_request_id", "audit_log", ["request_id"], unique=False)
    if "ix_audit_log_trace_id" not in index_names:
        op.create_index("ix_audit_log_trace_id", "audit_log", ["trace_id"], unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("audit_log")}
    index_names = {index["name"] for index in inspector.get_indexes("audit_log")}
    if "ix_audit_log_trace_id" in index_names:
        op.drop_index("ix_audit_log_trace_id", table_name="audit_log")
    if "ix_audit_log_request_id" in index_names:
        op.drop_index("ix_audit_log_request_id", table_name="audit_log")
    if "trace_id" in columns:
        op.drop_column("audit_log", "trace_id")
    if "request_id" in columns:
        op.drop_column("audit_log", "request_id")
