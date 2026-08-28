import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.database import Base, Transaction, RecoveryPipeline, AuditLog, MessageLog
from app.simulator import create_single_failed_transaction, process_payment_recovery, process_customer_opt_out

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

def test_full_recovery_flow(db_session):
    # 1. Trigger simulated webhook failure
    tx, pipeline = create_single_failed_transaction(db_session, scenario_index=0) # UPI Timeout
    
    assert tx.status == "failed"
    assert pipeline.status == "active"
    assert pipeline.current_stage == 1

    # 2. Simulate process step (AI generates notification)
    # We will manually perform the pipeline processing logic to test its flow
    from app.agent import RecoveryAgent
    agent = RecoveryAgent()
    strategy = agent.run_diagnosis(db_session, pipeline, tx)
    
    assert strategy is not None
    assert strategy.suggested_channel in ["SMS", "Email", "WhatsApp"]
    assert "[recovery_url]" in strategy.message_content

    # Log sent message and update pipeline
    msg = MessageLog(
        pipeline_id=pipeline.id,
        channel=strategy.suggested_channel,
        content=strategy.message_content,
        status="delivered"
    )
    db_session.add(msg)
    pipeline.current_stage += 1
    db_session.commit()

    assert pipeline.current_stage == 2
    assert db_session.query(MessageLog).filter(MessageLog.pipeline_id == pipeline.id).count() == 1

    # 3. Simulate customer clicking the pay link and completing checkout
    success = process_payment_recovery(db_session, pipeline.id)
    assert success is True
    
    # Reload values from DB
    db_session.refresh(tx)
    db_session.refresh(pipeline)

    assert tx.status == "recovered"
    assert pipeline.status == "succeeded"

    # Check Audit Logs
    audits = db_session.query(AuditLog).filter(AuditLog.pipeline_id == pipeline.id).all()
    event_types = [a.event_type for a in audits]
    assert "WEBHOOK_RECEIVED" in event_types
    assert "PAYMENT_RECOVERED" in event_types

def test_customer_opt_out_flow(db_session):
    # 1. Trigger simulated webhook failure
    tx, pipeline = create_single_failed_transaction(db_session, scenario_index=1) # Insufficient funds
    
    # 2. Simulate customer opting out (replying STOP)
    success = process_customer_opt_out(db_session, pipeline.id)
    assert success is True
    
    db_session.refresh(tx)
    db_session.refresh(pipeline)

    assert tx.status == "abandoned"
    assert pipeline.status == "stopped_opt_out"

    # Check Audit Logs
    audits = db_session.query(AuditLog).filter(AuditLog.pipeline_id == pipeline.id).all()
    event_types = [a.event_type for a in audits]
    assert "STOPPING_RULE_TRIGGERED" in event_types
