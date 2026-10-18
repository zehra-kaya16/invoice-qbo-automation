"""Signed company integrations and transactional notification outbox."""
from alembic import op
import sqlalchemy as sa

revision = "113a1d0c2026"
down_revision = "112a1d0c2026"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("webhook_integrations",
        sa.Column('id', sa.String(length=36), primary_key=True, nullable=False),
        sa.Column('company_id', sa.UUID(as_uuid=False), sa.ForeignKey('companies.id'), primary_key=False, nullable=False),
        sa.Column('owner_user_id', sa.UUID(as_uuid=False), sa.ForeignKey('users.id'), primary_key=False, nullable=False),
        sa.Column('name', sa.String(length=100), primary_key=False, nullable=False),
        sa.Column('secret', sa.Text(), primary_key=False, nullable=False),
        sa.Column('enabled', sa.Boolean(), primary_key=False, nullable=False),
        sa.Column('auto_extract', sa.Boolean(), primary_key=False, nullable=False),
        sa.Column('created_at', sa.DateTime(), primary_key=False, nullable=False))
    op.create_index('ix_webhook_integrations_company_id', 'webhook_integrations', ['company_id'])
    op.create_table("webhook_receipts",
        sa.Column('id', sa.String(length=36), primary_key=True, nullable=False),
        sa.Column('integration_id', sa.String(length=36), sa.ForeignKey('webhook_integrations.id'), primary_key=False, nullable=False),
        sa.Column('company_id', sa.UUID(as_uuid=False), sa.ForeignKey('companies.id'), primary_key=False, nullable=False),
        sa.Column('event_id', sa.String(length=36), primary_key=False, nullable=False),
        sa.Column('digest', sa.String(length=64), primary_key=False, nullable=False),
        sa.Column('kind', sa.String(length=10), primary_key=False, nullable=False),
        sa.Column('status', sa.String(length=20), primary_key=False, nullable=False),
        sa.Column('auto_extract', sa.Boolean(), primary_key=False, nullable=False),
        sa.Column('batch_id', sa.String(length=50), sa.ForeignKey('upload_batches.id'), primary_key=False, nullable=True),
        sa.Column('run_id', sa.String(length=50), sa.ForeignKey('batch_extraction_runs.id'), primary_key=False, nullable=True),
        sa.Column('created_at', sa.DateTime(), primary_key=False, nullable=False),
        sa.UniqueConstraint('integration_id', 'event_id', name='uq_webhook_event'))
    op.create_index('ix_webhook_receipts_company_id', 'webhook_receipts', ['company_id'])
    op.create_table("notification_preferences",
        sa.Column('user_id', sa.UUID(as_uuid=False), sa.ForeignKey('users.id'), primary_key=True, nullable=False),
        sa.Column('company_id', sa.UUID(as_uuid=False), sa.ForeignKey('companies.id'), primary_key=False, nullable=False),
        sa.Column('enabled', sa.Boolean(), primary_key=False, nullable=False),
        sa.Column('recipient_email', sa.String(length=255), primary_key=False, nullable=False))
    op.create_table("notification_outbox",
        sa.Column('id', sa.String(length=36), primary_key=True, nullable=False),
        sa.Column('company_id', sa.UUID(as_uuid=False), sa.ForeignKey('companies.id'), primary_key=False, nullable=False),
        sa.Column('user_id', sa.UUID(as_uuid=False), sa.ForeignKey('users.id'), primary_key=False, nullable=False),
        sa.Column('recipient_email', sa.String(length=255), primary_key=False, nullable=False),
        sa.Column('event_key', sa.String(length=255), primary_key=False, nullable=False),
        sa.Column('kind', sa.String(length=64), primary_key=False, nullable=False),
        sa.Column('document_id', sa.String(length=120), primary_key=False, nullable=False),
        sa.Column('filename', sa.String(length=255), primary_key=False, nullable=False),
        sa.Column('status', sa.String(length=24), primary_key=False, nullable=False),
        sa.Column('claim_token', sa.String(length=32), primary_key=False, nullable=True),
        sa.Column('provider_id', sa.String(length=255), primary_key=False, nullable=True),
        sa.Column('created_at', sa.DateTime(), primary_key=False, nullable=False),
        sa.Column('attempted_at', sa.DateTime(), primary_key=False, nullable=True),
        sa.Column('finished_at', sa.DateTime(), primary_key=False, nullable=True),
        sa.UniqueConstraint('user_id', 'event_key', name='uq_notification_event'))
    op.create_index('ix_notification_pending', 'notification_outbox', ['status', 'created_at'])


def downgrade():
    op.drop_index('ix_notification_pending', table_name='notification_outbox')
    op.drop_table('notification_outbox')
    op.drop_table('notification_preferences')
    op.drop_index('ix_webhook_receipts_company_id', table_name='webhook_receipts')
    op.drop_table('webhook_receipts')
    op.drop_index('ix_webhook_integrations_company_id', table_name='webhook_integrations')
    op.drop_table('webhook_integrations')
