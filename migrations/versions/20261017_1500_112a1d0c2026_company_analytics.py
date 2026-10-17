"""Durable company-scoped document and request metrics."""
from alembic import op
import sqlalchemy as sa

revision = "112a1d0c2026"
down_revision = "107a1d0c2026"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("analytics_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("company_id", sa.UUID(as_uuid=False), sa.ForeignKey("companies.id"), nullable=False),
        sa.Column("event_key", sa.String(255), nullable=False),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("document_id", sa.String(120)),
        sa.Column("value", sa.Float()),
        sa.Column("duration_ms", sa.Float()),
        sa.Column("request_method", sa.String(10)),
        sa.Column("status_code", sa.Integer()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("company_id", "event_key", name="uq_analytics_event_key"))
    op.create_index("ix_analytics_company_time_name", "analytics_events", ["company_id", "created_at", "name"])


def downgrade():
    op.drop_index("ix_analytics_company_time_name", table_name="analytics_events")
    op.drop_table("analytics_events")
