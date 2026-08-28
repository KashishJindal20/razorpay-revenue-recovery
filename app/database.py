import os
from datetime import datetime
from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime, ForeignKey
from sqlalchemy.orm import declarative_base, sessionmaker, relationship

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./revenue_recovery.db")

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

class Transaction(Base):
    __tablename__ = "transactions"

    id = Column(Integer, primary_key=True, index=True)
    order_id = Column(String, unique=True, index=True, nullable=False)
    amount = Column(Float, nullable=False)  # in INR
    currency = Column(String, default="INR")
    customer_name = Column(String, nullable=False)
    customer_email = Column(String, nullable=False)
    customer_phone = Column(String, nullable=False)
    status = Column(String, default="failed")  # failed, recovered, abandoned
    error_code = Column(String, nullable=True)
    error_description = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    pipelines = relationship("RecoveryPipeline", back_populates="transaction")


class RecoveryPipeline(Base):
    __tablename__ = "recovery_pipelines"

    id = Column(Integer, primary_key=True, index=True)
    transaction_id = Column(Integer, ForeignKey("transactions.id"), nullable=False)
    current_stage = Column(Integer, default=1)  # 1, 2, 3
    max_stages = Column(Integer, default=3)
    status = Column(String, default="active")  # active, succeeded, failed_max_retries, stopped_opt_out
    last_contacted_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    transaction = relationship("Transaction", back_populates="pipelines")
    audit_logs = relationship("AuditLog", back_populates="pipeline", cascade="all, delete-orphan")
    message_logs = relationship("MessageLog", back_populates="pipeline", cascade="all, delete-orphan")


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True, index=True)
    pipeline_id = Column(Integer, ForeignKey("recovery_pipelines.id"), nullable=False)
    event_type = Column(String, nullable=False)  # e.g., WEBHOOK_RECEIVED, AGENT_DIAGNOSIS, MESSAGE_SENT, PAYMENT_RECOVERED, STOPPING_RULE_TRIGGERED
    description = Column(String, nullable=False)
    timestamp = Column(DateTime, default=datetime.utcnow)

    # Relationships
    pipeline = relationship("RecoveryPipeline", back_populates="audit_logs")


class MessageLog(Base):
    __tablename__ = "message_logs"

    id = Column(Integer, primary_key=True, index=True)
    pipeline_id = Column(Integer, ForeignKey("recovery_pipelines.id"), nullable=False)
    channel = Column(String, nullable=False)  # SMS, Email, WhatsApp
    content = Column(String, nullable=False)
    status = Column(String, default="sent")  # sent, delivered, failed
    sent_at = Column(DateTime, default=datetime.utcnow)

    # Relationships
    pipeline = relationship("RecoveryPipeline", back_populates="message_logs")


def init_db():
    Base.metadata.create_all(bind=engine)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
