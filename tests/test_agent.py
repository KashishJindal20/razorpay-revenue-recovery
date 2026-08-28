import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.database import Base, Transaction, RecoveryPipeline, AuditLog, MessageLog
from app.agent import RecoveryAgent

# Set up clean in-memory database for testing
TEST_DATABASE_URL = "sqlite:///:memory:"
engine = create_engine(TEST_DATABASE_URL, connect_args={"check_same_thread": False})
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

@pytest.fixture(name="db_session")
def fixture_db_session():
    Base.metadata.create_all(bind=engine)
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)

def test_stopping_rules_max_attempts(db_session):
    # 1. Setup a transaction that has failed
    tx = Transaction(
        order_id="order_test_123",
        amount=500.0,
        customer_name="Test Customer",
        customer_email="test@example.com",
        customer_phone="+919999999999",
        status="failed",
        error_code="BAD_REQUEST_UPI_TIMEOUT",
        error_description="UPI Timeout"
    )
    db_session.add(tx)
    db_session.commit()

    # 2. Setup a pipeline that has already run 3 stages (stage 4)
    pipeline = RecoveryPipeline(
        transaction_id=tx.id,
        current_stage=4,  # greater than max_stages (3)
        max_stages=3,
        status="active"
    )
    db_session.add(pipeline)
    db_session.commit()

    agent = RecoveryAgent()
    strategy = agent.run_diagnosis(db_session, pipeline, tx)

    # 3. Verify that agent stops the pipeline
    assert strategy is None
    assert pipeline.status == "failed_max_retries"
    
    # 4. Verify audit log reflects stopping
    audit_logs = db_session.query(AuditLog).filter(AuditLog.pipeline_id == pipeline.id).all()
    assert len(audit_logs) == 1
    assert "STOPPING_RULE_TRIGGERED" in audit_logs[0].event_type

def test_stopping_rules_opt_out(db_session):
    tx = Transaction(
        order_id="order_test_456",
        amount=1500.0,
        customer_name="Opt Out Customer",
        customer_email="optout@example.com",
        customer_phone="+919999999999",
        status="failed",
        error_code="BAD_REQUEST_INSUFFICIENT_FUNDS",
        error_description="Insufficient funds"
    )
    db_session.add(tx)
    db_session.commit()

    pipeline = RecoveryPipeline(
        transaction_id=tx.id,
        current_stage=1,
        max_stages=3,
        status="stopped_opt_out"
    )
    db_session.add(pipeline)
    db_session.commit()

    agent = RecoveryAgent()
    strategy = agent.run_diagnosis(db_session, pipeline, tx)

    # Verify agent stops immediately due to opt out
    assert strategy is None
    audit_logs = db_session.query(AuditLog).filter(AuditLog.pipeline_id == pipeline.id).all()
    assert len(audit_logs) == 1
    assert "STOPPING_RULE_TRIGGERED" in audit_logs[0].event_type
