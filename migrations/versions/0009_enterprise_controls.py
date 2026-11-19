"""Add object lifecycle, application scopes, and index approval metadata."""

import json

import sqlalchemy as sa
from alembic import op

revision = "0009_enterprise_controls"
down_revision = "0008_job_credential"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    app_columns = _columns("app")
    if "permissions" not in app_columns:
        with op.batch_alter_table("app") as batch_op:
            batch_op.add_column(sa.Column("permissions", sa.JSON(), nullable=True))
        op.execute(
            sa.text(
                "UPDATE app SET permissions = :permissions WHERE permissions IS NULL"
            ).bindparams(permissions=json.dumps(["app:invoke"]))
        )
        with op.batch_alter_table("app") as batch_op:
            batch_op.alter_column(
                "permissions",
                existing_type=sa.JSON(),
                nullable=False,
            )

    version_columns = _columns("document_version")
    additions = {
        "original_filename": sa.Column("original_filename", sa.String(512)),
        "content_type": sa.Column("content_type", sa.String(255)),
        "object_size": sa.Column("object_size", sa.BigInteger()),
        "storage_status": sa.Column(
            "storage_status", sa.String(24), nullable=False, server_default="LEGACY_MISSING"
        ),
        "retention_until": sa.Column("retention_until", sa.DateTime()),
        "deleted_at": sa.Column("deleted_at", sa.DateTime()),
    }
    for name, column in additions.items():
        if name not in version_columns:
            op.add_column("document_version", column)

    index_columns = _columns("index_version")
    index_additions = {
        "approved_by": sa.Column("approved_by", sa.String(256)),
        "approved_at": sa.Column("approved_at", sa.DateTime()),
        "rejection_reason": sa.Column("rejection_reason", sa.String(1024)),
        "cleanup_status": sa.Column(
            "cleanup_status", sa.String(24), nullable=False, server_default="NOT_REQUIRED"
        ),
    }
    for name, column in index_additions.items():
        if name not in index_columns:
            op.add_column("index_version", column)


def downgrade() -> None:
    # These fields carry security and recovery evidence and are intentionally retained.
    pass
