"""Record job ownership for tenant-internal access control."""

import sqlalchemy as sa
from alembic import op

revision = "0007_job_creator"
down_revision = "0006_kb_version_snapshot"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("job")}
    if "creator_kind" not in columns:
        op.add_column("job", sa.Column("creator_kind", sa.String(16), nullable=True))
    if "creator_id" not in columns:
        op.add_column("job", sa.Column("creator_id", sa.String(256), nullable=True))


def downgrade() -> None:
    # Preserve ownership metadata; dropping it would weaken access control.
    pass
