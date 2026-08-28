import os
import json
from datetime import datetime
from fastapi import FastAPI, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session
from typing import List, Optional
from pydantic import BaseModel
import razorpay

from app.database import init_db, get_db, Transaction, RecoveryPipeline, AuditLog, MessageLog
from app.agent import RecoveryAgent
from app.simulator import (
    create_single_failed_transaction,
    create_batch_failures,
    process_payment_recovery,
    process_customer_opt_out
)

# Initialize database
init_db()

# Initialize Razorpay Client if credentials are provided
RAZORPAY_KEY_ID = os.environ.get("RAZORPAY_KEY_ID")
RAZORPAY_KEY_SECRET = os.environ.get("RAZORPAY_KEY_SECRET")
razorpay_client = None

if RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET:
    try:
        razorpay_client = razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))
        print("Razorpay client configured successfully.")
    except Exception as e:
        print(f"Error configuring Razorpay client: {e}")

app = FastAPI(title="AI Revenue Recovery System")
agent = RecoveryAgent()

# Ensure template directory exists
os.makedirs("templates", exist_ok=True)

# Pydantic schemas for API response serialization
class TransactionSchema(BaseModel):
    id: int
    order_id: str
    amount: float
    currency: str
    customer_name: str
    customer_email: str
    customer_phone: str
    status: str
    error_code: Optional[str]
    error_description: Optional[str]

    class Config:
        from_attributes = True

class MessageLogSchema(BaseModel):
    id: int
    channel: str
    content: str
    status: str
    sent_at: str

    class Config:
        from_attributes = True

class AuditLogSchema(BaseModel):
    id: int
    event_type: str
    description: str
    timestamp: str

    class Config:
        from_attributes = True

class PipelineSchema(BaseModel):
    id: int
    transaction_id: int
    current_stage: int
    max_stages: int
    status: str
    created_at: str
    updated_at: str
    transaction: TransactionSchema
    audit_logs: List[AuditLogSchema] = []
    message_logs: List[MessageLogSchema] = []

    class Config:
        from_attributes = True

# Helper to format datetime
def format_dt(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else None

@app.get("/", response_class=HTMLResponse)
def get_dashboard():
    template_path = os.path.join("templates", "index.html")
    if os.path.exists(template_path):
        with open(template_path, "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read())
    return HTMLResponse(content="<h1>Dashboard Template Not Found. Please create templates/index.html</h1>", status_code=404)

@app.get("/api/stats")
def get_stats(db: Session = Depends(get_db)):
    txs = db.query(Transaction).all()
    pipelines = db.query(RecoveryPipeline).all()
    
    total_leaked = sum(t.amount for t in txs if t.status in ["failed", "abandoned"])
    total_recovered = sum(t.amount for t in txs if t.status == "recovered")
    total_attempted = sum(t.amount for t in txs)
    
    recovery_rate = (total_recovered / total_attempted * 100) if total_attempted > 0 else 0.0
    
    stats = {
        "total_leaked": round(total_leaked, 2),
        "total_recovered": round(total_recovered, 2),
        "recovery_rate": round(recovery_rate, 2),
        "total_pipelines": len(pipelines),
        "active_pipelines": len([p for p in pipelines if p.status == "active"]),
        "succeeded_pipelines": len([p for p in pipelines if p.status == "succeeded"]),
        "optout_pipelines": len([p for p in pipelines if p.status == "stopped_opt_out"]),
        "failed_pipelines": len([p for p in pipelines if p.status == "failed_max_retries"])
    }
    return stats

@app.get("/api/pipelines")
def get_pipelines(db: Session = Depends(get_db)):
    pipelines = db.query(RecoveryPipeline).order_by(RecoveryPipeline.created_at.desc()).all()
    res = []
    for p in pipelines:
        tx = p.transaction
        res.append({
            "id": p.id,
            "transaction_id": p.transaction_id,
            "current_stage": p.current_stage,
            "max_stages": p.max_stages,
            "status": p.status,
            "created_at": format_dt(p.created_at),
            "updated_at": format_dt(p.updated_at),
            "transaction": {
                "id": tx.id,
                "order_id": tx.order_id,
                "amount": tx.amount,
                "currency": tx.currency,
                "customer_name": tx.customer_name,
                "customer_email": tx.customer_email,
                "customer_phone": tx.customer_phone,
                "status": tx.status,
                "error_code": tx.error_code,
                "error_description": tx.error_description
            }
        })
    return res

@app.get("/api/pipelines/{pipeline_id}")
def get_pipeline_details(pipeline_id: int, db: Session = Depends(get_db)):
    pipeline = db.query(RecoveryPipeline).filter(RecoveryPipeline.id == pipeline_id).first()
    if not pipeline:
        raise HTTPException(status_code=404, detail="Pipeline not found")
        
    tx = pipeline.transaction
    audits = db.query(AuditLog).filter(AuditLog.pipeline_id == pipeline.id).order_by(AuditLog.timestamp.desc()).all()
    messages = db.query(MessageLog).filter(MessageLog.pipeline_id == pipeline.id).order_by(MessageLog.sent_at.desc()).all()
    
    return {
        "id": pipeline.id,
        "transaction_id": pipeline.transaction_id,
        "current_stage": pipeline.current_stage,
        "max_stages": pipeline.max_stages,
        "status": pipeline.status,
        "created_at": format_dt(pipeline.created_at),
        "updated_at": format_dt(pipeline.updated_at),
        "transaction": {
            "id": tx.id,
            "order_id": tx.order_id,
            "amount": tx.amount,
            "currency": tx.currency,
            "customer_name": tx.customer_name,
            "customer_email": tx.customer_email,
            "customer_phone": tx.customer_phone,
            "status": tx.status,
            "error_code": tx.error_code,
            "error_description": tx.error_description
        },
        "audit_logs": [
            {
                "id": a.id,
                "event_type": a.event_type,
                "description": a.description,
                "timestamp": format_dt(a.timestamp)
            } for a in audits
        ],
        "message_logs": [
            {
                "id": m.id,
                "channel": m.channel,
                "content": m.content,
                "status": m.status,
                "sent_at": format_dt(m.sent_at)
            } for m in messages
        ]
    }

@app.post("/api/simulator/trigger-single")
def trigger_single(scenario_index: Optional[int] = Query(None), db: Session = Depends(get_db)):
    tx, pipeline = create_single_failed_transaction(db, scenario_index)
    return {"message": "Failure simulated", "order_id": tx.order_id, "pipeline_id": pipeline.id}

@app.post("/api/simulator/trigger-batch")
def trigger_batch(count: int = 20, db: Session = Depends(get_db)):
    results = create_batch_failures(db, count)
    return {"message": f"Batch of {count} failures simulated successfully"}

@app.post("/api/simulator/pay/{pipeline_id}")
def simulate_pay(pipeline_id: int, db: Session = Depends(get_db)):
    success = process_payment_recovery(db, pipeline_id)
    if not success:
        raise HTTPException(status_code=400, detail="Cannot pay for inactive/non-existent pipeline")
    return {"message": "Payment simulation successful"}

@app.post("/api/simulator/optout/{pipeline_id}")
def simulate_optout(pipeline_id: int, db: Session = Depends(get_db)):
    success = process_customer_opt_out(db, pipeline_id)
    if not success:
        raise HTTPException(status_code=400, detail="Cannot opt out of inactive/non-existent pipeline")
    return {"message": "Opt-out simulation successful"}

@app.post("/api/pipelines/process-active")
def process_active_pipelines(db: Session = Depends(get_db)):
    """
    Ticks the state machine forward: runs the AI agent diagnosis for all active pipelines,
    schedules the recovery communication and advances the pipeline stage.
    """
    active_pipelines = db.query(RecoveryPipeline).filter(RecoveryPipeline.status == "active").all()
    processed_count = 0
    
    for pipeline in active_pipelines:
        tx = pipeline.transaction
        
        # Run AI Diagnosis
        strategy = agent.run_diagnosis(db, pipeline, tx)
        
        if strategy is None:
            # Pipeline was stopped by agent rules (max stage or opt out)
            continue
            
        # Generate recovery URL (use real Razorpay if configured, else fallback to mock)
        recovery_url = f"http://localhost:8000/?pay_pipeline_id={pipeline.id}"
        if razorpay_client is not None:
            try:
                link_data = {
                    "amount": int(tx.amount * 100), # in paise
                    "currency": "INR",
                    "accept_partial": False,
                    "description": f"AI Recovery for Order {tx.order_id}",
                    "customer": {
                        "name": tx.customer_name,
                        "email": tx.customer_email,
                        "contact": tx.customer_phone
                    },
                    "notify": {
                        "sms": False,
                        "email": False
                    },
                    "reminder_enable": False,
                    "notes": {
                        "pipeline_id": str(pipeline.id)
                    },
                    "callback_url": f"http://localhost:8000/api/recovery/callback?pipeline_id={pipeline.id}",
                    "callback_method": "get"
                }
                payment_link = razorpay_client.payment_link.create(link_data)
                recovery_url = payment_link.get("short_url", recovery_url)
            except Exception as e:
                print(f"Error creating Razorpay Payment Link: {e}. Falling back to mock link.")

        custom_message = strategy.message_content.replace("[recovery_url]", recovery_url)
        
        # Save message log
        msg_log = MessageLog(
            pipeline_id=pipeline.id,
            channel=strategy.suggested_channel,
            content=custom_message,
            status="delivered"
        )
        db.add(msg_log)
        
        # Log to audit trail
        audit = AuditLog(
            pipeline_id=pipeline.id,
            event_type="MESSAGE_SENT",
            description=f"Sent {strategy.suggested_channel} message to {tx.customer_name} ({tx.customer_phone if strategy.suggested_channel != 'Email' else tx.customer_email})."
        )
        db.add(audit)
        
        # Advance pipeline stage
        pipeline.current_stage += 1
        pipeline.last_contacted_at = datetime.utcnow()
        db.commit()
        
        processed_count += 1
        
    return {"message": f"Successfully processed {processed_count} active pipelines."}

@app.get("/api/recovery/callback")
def recovery_callback(pipeline_id: int, razorpay_payment_id: Optional[str] = None, db: Session = Depends(get_db)):
    """
    Handles payment callback redirection from Razorpay hosted page.
    """
    if razorpay_payment_id:
        success = process_payment_recovery(db, pipeline_id)
        if success:
            return HTMLResponse(content=f"""
                <html>
                <head>
                    <title>Payment Successful</title>
                    <script src="https://cdn.tailwindcss.com"></script>
                </head>
                <body class="bg-gray-50 min-h-screen flex items-center justify-center font-sans">
                    <div class="bg-white p-8 rounded-lg shadow-md border border-emerald-100 max-w-md text-center">
                        <div class="w-16 h-16 bg-emerald-50 rounded-full flex items-center justify-center text-emerald-500 mx-auto mb-4">
                            <svg class="w-8 h-8" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7"></path>
                            </svg>
                        </div>
                        <h1 class="text-xl font-bold text-gray-900 mb-2">Payment Recovered!</h1>
                        <p class="text-sm text-gray-600 mb-6 font-medium">Thank you, your payment has been processed successfully. Your order is now active.</p>
                        <a href="http://localhost:8000" class="bg-indigo-600 hover:bg-indigo-700 text-white text-sm font-bold py-2 px-6 rounded transition">Go to Dashboard</a>
                    </div>
                </body>
                </html>
            """)
    raise HTTPException(status_code=400, detail="Payment verification failed")

@app.post("/api/webhooks/razorpay")
async def razorpay_webhook(request: Request, db: Session = Depends(get_db)):
    """
    Receives events from Razorpay (payment.failed, payment_link.paid, payment.captured).
    """
    body = await request.body()
    try:
        payload = json.loads(body.decode())
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    # Optional signature verification
    webhook_secret = os.environ.get("RAZORPAY_WEBHOOK_SECRET")
    if webhook_secret and razorpay_client:
        signature = request.headers.get("X-Razorpay-Signature")
        if not signature:
            raise HTTPException(status_code=400, detail="Missing signature header")
        try:
            razorpay_client.utility.verify_webhook_signature(body.decode(), signature, webhook_secret)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Invalid webhook signature: {e}")

    event = payload.get("event")
    print(f"Received Razorpay Webhook Event: {event}")

    if event == "payment.failed":
        payment_entity = payload.get("payload", {}).get("payment", {}).get("entity", {})
        order_id = payment_entity.get("order_id") or f"order_real_{payment_entity.get('id')}"
        amount = float(payment_entity.get("amount", 0)) / 100.0
        
        customer_name = payment_entity.get("notes", {}).get("customer_name") or "Razorpay Customer"
        customer_email = payment_entity.get("email") or "customer@example.com"
        customer_phone = payment_entity.get("contact") or "+919999999999"
        
        error_code = payment_entity.get("error_code") or "BAD_REQUEST_PAYMENT_FAILED"
        error_description = payment_entity.get("error_description") or "Payment declined by provider bank."
        
        tx = db.query(Transaction).filter(Transaction.order_id == order_id).first()
        if not tx:
            tx = Transaction(
                order_id=order_id,
                amount=amount,
                customer_name=customer_name,
                customer_email=customer_email,
                customer_phone=customer_phone,
                status="failed",
                error_code=error_code,
                error_description=error_description
            )
            db.add(tx)
            db.commit()
            db.refresh(tx)
            
            pipeline = RecoveryPipeline(
                transaction_id=tx.id,
                current_stage=1,
                max_stages=3,
                status="active"
            )
            db.add(pipeline)
            db.commit()
            db.refresh(pipeline)
            
            audit = AuditLog(
                pipeline_id=pipeline.id,
                event_type="WEBHOOK_RECEIVED",
                description=f"Received REAL webhook for failed order {tx.order_id}. Error: {tx.error_code} - {tx.error_description}"
            )
            db.add(audit)
            db.commit()
            
    elif event in ["payment_link.paid", "payment.captured"]:
        pipeline_id = None
        
        if event == "payment_link.paid":
            link_entity = payload.get("payload", {}).get("payment_link", {}).get("entity", {})
            pipeline_id_str = link_entity.get("notes", {}).get("pipeline_id")
            if pipeline_id_str:
                pipeline_id = int(pipeline_id_str)
        elif event == "payment.captured":
            payment_entity = payload.get("payload", {}).get("payment", {}).get("entity", {})
            order_id = payment_entity.get("order_id")
            if order_id:
                tx = db.query(Transaction).filter(Transaction.order_id == order_id).first()
                if tx:
                    pipeline = db.query(RecoveryPipeline).filter(RecoveryPipeline.transaction_id == tx.id).first()
                    if pipeline:
                        pipeline_id = pipeline.id
                        
        if pipeline_id:
            success = process_payment_recovery(db, pipeline_id)
            if success:
                print(f"Pipeline {pipeline_id} successfully marked as RECOVERED via Webhook.")

    return {"status": "ok"}
