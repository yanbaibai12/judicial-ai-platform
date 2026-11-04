"""Record the application credential used to create asynchronous jobs."""

import sqlalchemy as sa
from alembic import op

revision = "0008_job_credential"
down_revision = "0007_job_creator"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("job")}
    if "creator_credential_id" not in columns:
        op.add_column("job", sa.Column("creator_credential_id", sa.BigInteger(), nullable=True))


def downgrade() -> None:
    # Preserve the metadata required to revalidate queued jobs.
    pass
