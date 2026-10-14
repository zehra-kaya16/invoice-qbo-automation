"""Persistent company-scoped upload batches."""
from alembic import op
import sqlalchemy as sa

revision = "104a1d0c2026"
down_revision = "100a1d0c2026"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("upload_batches",
        sa.Column("id", sa.String(50), primary_key=True),
        sa.Column("company_id", sa.Uuid(as_uuid=False), sa.ForeignKey("companies.id"), nullable=False),
        sa.Column("request_key", sa.String(36), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("total_files", sa.Integer(), nullable=False),
        sa.Column("document_ids", sa.JSON(), nullable=False),
        sa.Column("failed_files", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("company_id", "request_key", name="uq_batch_company_request"))
    op.create_index("ix_upload_batches_company_id", "upload_batches", ["company_id"])


def downgrade():
    op.drop_index("ix_upload_batches_company_id", table_name="upload_batches")
    op.drop_table("upload_batches")
