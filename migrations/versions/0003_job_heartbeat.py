"""Add worker heartbeat timestamp.

Revision ID: 0003_job_heartbeat
Revises: 0002_resource_grant
"""

import sqlalchemy as sa
from alembic import op

revision = "0003_job_heartbeat"
down_revision = "0002_resource_grant"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "heartbeat_at" not in {column["name"] for column in inspector.get_columns("job")}:
        op.add_column("job", sa.Column("heartbeat_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "heartbeat_at" in {column["name"] for column in inspector.get_columns("job")}:
        op.drop_column("job", "heartbeat_at")
