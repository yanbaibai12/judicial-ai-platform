"""Store immutable parsed-content snapshots for newly created knowledge versions."""

import sqlalchemy as sa
from alembic import op

revision = "0006_kb_version_snapshot"
down_revision = "0005_document_access_policy"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "snapshot_state" not in {column["name"] for column in inspector.get_columns("kb_version")}:
        op.add_column(
            "kb_version",
            sa.Column("snapshot_state", sa.String(16), nullable=False, server_default="LEGACY"),
        )
    if "kb_version_chunk" not in inspector.get_table_names():
        op.create_table(
            "kb_version_chunk",
            sa.Column("kb_version_id", sa.BigInteger(), primary_key=True),
            sa.Column("chunk_id", sa.String(64), primary_key=True),
            sa.Column("payload", sa.JSON(), nullable=False),
            sa.Column("document", sa.JSON(), nullable=False),
        )


def downgrade() -> None:
    # Removing snapshots would make historical rebuilds non-reproducible.
    pass
