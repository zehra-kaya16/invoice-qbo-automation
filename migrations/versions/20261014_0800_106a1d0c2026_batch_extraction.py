"""Durable batch extraction runs."""
from alembic import op
import sqlalchemy as sa

revision = "106a1d0c2026"
down_revision = "104a1d0c2026"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("batch_extraction_runs",
        sa.Column("id", sa.String(50), primary_key=True),
        sa.Column("batch_id", sa.String(50), sa.ForeignKey("upload_batches.id"), nullable=False),
        sa.Column("company_id", sa.UUID(as_uuid=False), sa.ForeignKey("companies.id"), nullable=False),
        sa.Column("owner_user_id", sa.UUID(as_uuid=False), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("owner_session_hash", sa.String(64), nullable=False),
        sa.Column("request_key", sa.String(36), nullable=False),
        sa.Column("retry_failed", sa.Boolean(), nullable=False),
        sa.Column("active_key", sa.String(10), nullable=True),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("claim_token", sa.String(32), nullable=True),
        sa.Column("items", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.UniqueConstraint("company_id", "request_key", name="uq_batch_run_request"),
        sa.UniqueConstraint("batch_id", "active_key", name="uq_batch_run_active"))
    op.create_index("ix_batch_extraction_runs_company_id", "batch_extraction_runs", ["company_id"])
    op.create_index("ix_batch_extraction_runs_status", "batch_extraction_runs", ["status"])


def downgrade():
    op.drop_index("ix_batch_extraction_runs_status", table_name="batch_extraction_runs")
    op.drop_index("ix_batch_extraction_runs_company_id", table_name="batch_extraction_runs")
    op.drop_table("batch_extraction_runs")
