"""
Database models for Receipt AI.

Uses SQLAlchemy for ORM with PostgreSQL.
"""

from datetime import datetime
from typing import Optional, List
from decimal import Decimal
import uuid

from sqlalchemy import (
    Column, String, Integer, Float, Boolean, DateTime,
    ForeignKey, Text, Enum, JSON, Numeric
)
from sqlalchemy.orm import declarative_base, relationship
from sqlalchemy.dialects.postgresql import UUID

from app.schemas.documents import DocumentType, ProcessingStatus


Base = declarative_base()


def generate_uuid():
    return str(uuid.uuid4())


class Company(Base):
    """
    Company/Organization - the customer account.
    One company can have multiple users and QBO connections.
    """
    __tablename__ = "companies"
    
    id = Column(UUID(as_uuid=False), primary_key=True, default=generate_uuid)
    name = Column(String(255), nullable=False)
    email = Column(String(255))
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Subscription
    subscription_tier = Column(String(50), default="starter")  # starter, pro, firm
    subscription_status = Column(String(50), default="trial")  # trial, active, canceled
    stripe_customer_id = Column(String(255))
    stripe_subscription_id = Column(String(255))
    
    # Relationships
    users = relationship("User", back_populates="company")
    qbo_connections = relationship("QBOConnection", back_populates="company")
    documents = relationship("Document", back_populates="company")


class User(Base):
    """
    User account within a company.
    """
    __tablename__ = "users"
    
    id = Column(UUID(as_uuid=False), primary_key=True, default=generate_uuid)
    company_id = Column(UUID(as_uuid=False), ForeignKey("companies.id"), nullable=False)
    email = Column(String(255), unique=True, nullable=False)
    password_hash = Column(String(255))
    name = Column(String(255))
    role = Column(String(50), default="user")  # admin, user
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    last_login = Column(DateTime)
    
    # Relationships
    company = relationship("Company", back_populates="users")


class QBOConnection(Base):
    """
    QuickBooks Online connection for a company.
    Stores OAuth tokens and company mapping.
    """
    __tablename__ = "qbo_connections"
    
    id = Column(UUID(as_uuid=False), primary_key=True, default=generate_uuid)
    company_id = Column(UUID(as_uuid=False), ForeignKey("companies.id"), nullable=False)
    
    # QBO identifiers
    realm_id = Column(String(50), unique=True, nullable=False)
    qbo_company_name = Column(String(255))
    
    # OAuth tokens (encrypted in production)
    access_token = Column(Text)
    refresh_token = Column(Text)
    token_expires_at = Column(DateTime)
    
    # Status
    is_active = Column(Boolean, default=True)
    last_sync = Column(DateTime)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Relationships
    company = relationship("Company", back_populates="qbo_connections")


class Document(Base):
    """
    Uploaded document (receipt, bank statement, etc.)
    """
    __tablename__ = "documents"
    
    id = Column(UUID(as_uuid=False), primary_key=True, default=generate_uuid)
    company_id = Column(UUID(as_uuid=False), ForeignKey("companies.id"), nullable=False)
    
    # File info
    filename = Column(String(255), nullable=False)
    content_type = Column(String(100))
    file_size = Column(Integer)
    storage_key = Column(String(500))  # S3/R2 path
    
    # Processing
    document_type = Column(String(50))  # receipt, invoice, bank_statement, check
    status = Column(String(50), default="uploaded")
    error_message = Column(Text)
    
    # Timestamps
    created_at = Column(DateTime, default=datetime.utcnow)
    processed_at = Column(DateTime)
    
    # Relationships
    company = relationship("Company", back_populates="documents")
    extracted_data = relationship("ExtractedData", back_populates="document", uselist=False)
    transactions = relationship("ExtractedTransaction", back_populates="document")


class ExtractedData(Base):
    """
    Extracted data from a document (receipts/invoices).
    """
    __tablename__ = "extracted_data"
    
    id = Column(UUID(as_uuid=False), primary_key=True, default=generate_uuid)
    document_id = Column(UUID(as_uuid=False), ForeignKey("documents.id"), unique=True, nullable=False)
    
    # Common fields
    vendor_name = Column(String(255))
    vendor_address = Column(Text)
    date = Column(DateTime)
    total_amount = Column(Numeric(12, 2))
    subtotal = Column(Numeric(12, 2))
    tax_amount = Column(Numeric(12, 2))
    
    # Category
    category_suggestion = Column(String(100))
    category_id = Column(String(50))  # QBO account ID if matched
    
    # Vendor matching
    vendor_id = Column(String(50))  # QBO vendor ID if matched
    
    # AI extraction metadata
    confidence = Column(Float)
    raw_text = Column(Text)
    extraction_model = Column(String(100))
    
    # QBO sync
    qbo_entity_type = Column(String(50))  # expense, bill
    qbo_entity_id = Column(String(50))
    pushed_to_qbo_at = Column(DateTime)
    
    # Relationships
    document = relationship("Document", back_populates="extracted_data")
    line_items = relationship("LineItem", back_populates="extracted_data")


class LineItem(Base):
    """
    Line item from a receipt or invoice.
    """
    __tablename__ = "line_items"
    
    id = Column(UUID(as_uuid=False), primary_key=True, default=generate_uuid)
    extracted_data_id = Column(UUID(as_uuid=False), ForeignKey("extracted_data.id"), nullable=False)
    
    description = Column(String(500))
    quantity = Column(Float, default=1.0)
    unit_price = Column(Numeric(12, 2))
    amount = Column(Numeric(12, 2))
    category = Column(String(100))
    
    # Relationships
    extracted_data = relationship("ExtractedData", back_populates="line_items")


class ExtractedTransaction(Base):
    """
    Transaction extracted from a bank statement.
    """
    __tablename__ = "extracted_transactions"
    
    id = Column(UUID(as_uuid=False), primary_key=True, default=generate_uuid)
    document_id = Column(UUID(as_uuid=False), ForeignKey("documents.id"), nullable=False)
    
    # Transaction data
    date = Column(DateTime, nullable=False)
    description = Column(String(500))
    amount = Column(Numeric(12, 2), nullable=False)
    transaction_type = Column(String(50))  # debit, credit, check, deposit
    check_number = Column(String(50))
    running_balance = Column(Numeric(12, 2))
    
    # Vendor
    vendor_suggestion = Column(String(255))
    vendor_id = Column(String(50))  # QBO vendor ID if matched
    
    # Category
    category_suggestion = Column(String(100))
    category_id = Column(String(50))  # QBO account ID
    
    # Check image
    check_image_key = Column(String(500))  # S3/R2 path to snipped check
    
    # QBO matching
    qbo_transaction_id = Column(String(50))
    match_score = Column(Float)
    match_status = Column(String(50))  # matched, unmatched, suggested
    
    # Relationships
    document = relationship("Document", back_populates="transactions")


class AuditLog(Base):
    """
    Audit log for tracking important actions.
    """
    __tablename__ = "audit_logs"
    
    id = Column(UUID(as_uuid=False), primary_key=True, default=generate_uuid)
    company_id = Column(UUID(as_uuid=False), ForeignKey("companies.id"))
    user_id = Column(UUID(as_uuid=False), ForeignKey("users.id"))
    
    action = Column(String(100), nullable=False)
    entity_type = Column(String(50))
    entity_id = Column(String(50))
    details = Column(JSON)
    
    ip_address = Column(String(50))
    user_agent = Column(String(500))
    
    created_at = Column(DateTime, default=datetime.utcnow)
