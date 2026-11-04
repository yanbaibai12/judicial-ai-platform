"""Preserve the available chunk membership and embedding for each index."""

import sqlalchemy as sa
from alembic import op

revision = "0004_index_chunk_snapshot"
down_revision = "0003_job_heartbeat"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if "index_chunk_snapshot" not in sa.inspect(bind).get_table_names():
        op.create_table(
            "index_chunk_snapshot",
            sa.Column("index_version_id", sa.BigInteger(), primary_key=True),
            sa.Column("chunk_id", sa.String(length=64), primary_key=True),
            sa.Column("embedding", sa.JSON(), nullable=True),
        )
        op.create_index(
            "idx_index_chunk_snapshot_chunk", "index_chunk_snapshot", ["chunk_id"]
        )
    bind.execute(sa.text("""
        INSERT INTO index_chunk_snapshot (index_version_id, chunk_id, embedding)
        SELECT c.index_version_id, c.chunk_id, c.embedding
        FROM chunk_meta AS c
        WHERE c.index_version_id IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM index_chunk_snapshot AS s
              WHERE s.index_version_id = c.index_version_id AND s.chunk_id = c.chunk_id
          )
    """))


def downgrade() -> None:
    # Historical version membership cannot be reconstructed after dropping this table.
    pass
