"""Add explicit document access policy without changing existing access."""

import sqlalchemy as sa
from alembic import op

revision = "0005_document_access_policy"
down_revision = "0004_index_chunk_snapshot"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "access_policy" not in {
        column["name"] for column in sa.inspect(op.get_bind()).get_columns("document")
    }:
        op.add_column(
            "document",
            sa.Column("access_policy", sa.String(16), nullable=False, server_default="INHERIT"),
        )


def downgrade() -> None:
    # Dropping the policy would silently expose restricted documents.
    pass
