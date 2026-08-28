import random
from datetime import datetime, timedelta
from sqlalchemy.orm import Session
from app.database import Transaction, RecoveryPipeline, AuditLog, MessageLog

# Predefined datasets for realistic synthetic users in India
NAMES = ["Amit Sharma", "Priya Patel", "Rahul Verma", "Sneha Reddy", "Vikram Singh", 
         "Ananya Iyer", "Rajesh Gupta", "Meera Nair", "Siddharth Joshi", "Kriti Saxena"]

EMAILS = ["amit.sharma@example.com", "priya.patel@example.com", "rahul.v@example.com", 
          "sneha.r@example.com", "vikram.s@example.com", "ananya.i@example.com", 
          "rajesh.g@example.com", "meera.n@example.com", "sid.j@example.com", "kriti.s@example.com"]

PHONES = ["+919876543210", "+919812345678", "+919123456789", "+918877665544", "+919988776655",
          "+919555443322", "+919666778899", "+918111223344", "+917000112233", "+919444556677"]

SCENARIOS = [
    {
        "error_code": "BAD_REQUEST_UPI_TIMEOUT",
        "error_description": "UPI payment timed out at the provider bank side.",
        "amount_range": (150, 1500),
    },
    {
        "error_code": "BAD_REQUEST_INSUFFICIENT_FUNDS",
        "error_description": "The customer's card was declined due to insufficient funds.",
        "amount_range": (500, 4500),
    },
    {
        "error_code": "BAD_REQUEST_CART_ABANDONED",
        "error_description": "Customer completed checkout steps but closed the tab before choosing a payment instrument.",
        "amount_range": (1200, 15000),
    },
    {
        "error_code": "BAD_REQUEST_SUBSCRIPTION_DECLINED",
        "error_description": "The recurring auto-debit charge was declined by the issuing bank.",
        "amount_range": (499, 2999),
    }
]

def create_single_failed_transaction(db: Session, scenario_index: int = None) -> tuple[Transaction, RecoveryPipeline]:
    """
    Creates a single synthetic payment failure transaction and initializes its recovery pipeline.
    """
    idx = random.randint(0, len(NAMES) - 1)
    name = NAMES[idx]
    email = EMAILS[idx]
    phone = PHONES[idx]
    
    if scenario_index is not None and 0 <= scenario_index < len(SCENARIOS):
        scenario = SCENARIOS[scenario_index]
    else:
        scenario = random.choice(SCENARIOS)
        
    amount = float(random.randint(scenario["amount_range"][0], scenario["amount_range"][1]))
    order_id = f"order_{random.randint(100000, 999999)}"
    
    # 1. Create Transaction
    tx = Transaction(
        order_id=order_id,
        amount=amount,
        customer_name=name,
        customer_email=email,
        customer_phone=phone,
        status="failed",
        error_code=scenario["error_code"],
        error_description=scenario["error_description"]
    )
    db.add(tx)
    db.commit()
    db.refresh(tx)
    
    # 2. Create Recovery Pipeline
    pipeline = RecoveryPipeline(
        transaction_id=tx.id,
        current_stage=1,
        max_stages=3,
        status="active"
    )
    db.add(pipeline)
    db.commit()
    db.refresh(pipeline)
    
    # 3. Create initial webhook audit log
    audit = AuditLog(
        pipeline_id=pipeline.id,
        event_type="WEBHOOK_RECEIVED",
        description=f"Received webhook for failed order {tx.order_id}. Error: {tx.error_code} - {tx.error_description}"
    )
    db.add(audit)
    db.commit()
    
    return tx, pipeline

def create_batch_failures(db: Session, count: int = 20) -> list[tuple[Transaction, RecoveryPipeline]]:
    """
    Creates a batch of failed transactions.
    """
    results = []
    for _ in range(count):
        results.append(create_single_failed_transaction(db))
    return results

def process_payment_recovery(db: Session, pipeline_id: int) -> bool:
    """
    Simulates a user clicking the recovery link and completing the payment successfully.
    """
    pipeline = db.query(RecoveryPipeline).filter(RecoveryPipeline.id == pipeline_id).first()
    if not pipeline or pipeline.status != "active":
        return False
        
    # Mark pipeline as successfully recovered
    pipeline.status = "succeeded"
    pipeline.updated_at = datetime.utcnow()
    
    # Update transaction state
    tx = pipeline.transaction
    tx.status = "recovered"
    tx.updated_at = datetime.utcnow()
    
    # Log audit event
    audit = AuditLog(
        pipeline_id=pipeline.id,
        event_type="PAYMENT_RECOVERED",
        description=f"Customer clicked recovery link and completed payment of Rs.{tx.amount:.2f} successfully."
    )
    db.add(audit)
    db.commit()
    
    return True

def process_customer_opt_out(db: Session, pipeline_id: int) -> bool:
    """
    Simulates a user opting out of communications (replying STOP).
    """
    pipeline = db.query(RecoveryPipeline).filter(RecoveryPipeline.id == pipeline_id).first()
    if not pipeline or pipeline.status != "active":
        return False
        
    # Mark pipeline as stopped
    pipeline.status = "stopped_opt_out"
    pipeline.updated_at = datetime.utcnow()
    
    # Update transaction state to abandoned
    tx = pipeline.transaction
    tx.status = "abandoned"
    tx.updated_at = datetime.utcnow()
    
    # Log audit event
    audit = AuditLog(
        pipeline_id=pipeline.id,
        event_type="STOPPING_RULE_TRIGGERED",
        description="Customer replied STOP. Recovery pipeline aborted to ensure compliance."
    )
    db.add(audit)
    db.commit()
    
    return True
