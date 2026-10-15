"""Saved per-document mappings and durable bulk QBO push checkpoints."""
from alembic import op
import sqlalchemy as sa

revision = "107a1d0c2026"
down_revision = "106a1d0c2026"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("upload_batches", sa.Column("qbo_plans", sa.JSON(), nullable=False, server_default="{}"))
    op.add_column("upload_batches", sa.Column("revision", sa.Integer(), nullable=False, server_default="1"))
    op.create_table("batch_push_runs",
        sa.Column("id", sa.String(50), primary_key=True),
        sa.Column("batch_id", sa.String(50), sa.ForeignKey("upload_batches.id"), nullable=False),
        sa.Column("company_id", sa.UUID(as_uuid=False), sa.ForeignKey("companies.id"), nullable=False),
        sa.Column("request_key", sa.String(36), nullable=False),
        sa.Column("selection", sa.JSON(), nullable=False),
        sa.Column("items", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("active_key", sa.String(10), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.UniqueConstraint("company_id", "request_key", name="uq_batch_push_request"),
        sa.UniqueConstraint("batch_id", "active_key", name="uq_batch_push_active"))
    op.create_index("ix_batch_push_runs_company_id", "batch_push_runs", ["company_id"])


def downgrade():
    op.drop_index("ix_batch_push_runs_company_id", table_name="batch_push_runs")
    op.drop_table("batch_push_runs")
    op.drop_column("upload_batches", "revision")
    op.drop_column("upload_batches", "qbo_plans")
