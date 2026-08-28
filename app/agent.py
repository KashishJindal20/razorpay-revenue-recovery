import os
import json
import logging
from pydantic import BaseModel, Field
from typing import Optional, List
import google.generativeai as genai
from sqlalchemy.orm import Session
from app.database import Transaction, RecoveryPipeline, AuditLog, MessageLog

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Pydantic schema for the LLM structured output
class RecoveryStrategy(BaseModel):
    diagnosed_reason: str = Field(description="The diagnosed root cause of the payment failure.")
    suggested_channel: str = Field(description="The channel to use: 'SMS', 'Email', or 'WhatsApp'.")
    wait_time_minutes: int = Field(description="Delay in minutes before sending the notification. E.g., 0 for instant, 120 for 2 hours.")
    message_content: str = Field(description="The personalized, context-aware recovery message content. Write in engaging Hinglish for Indian phone numbers if appropriate, or English.")
    explanation: str = Field(description="Explanation of the reasoning behind this recovery plan.")

# Simple rule-based fallback agent if API key is not present
def get_rule_based_strategy(
    error_code: str, 
    customer_name: str, 
    amount: float, 
    channel: str = "SMS", 
    stage: int = 1
) -> RecoveryStrategy:
    error_code = (error_code or "").upper()
    
    # Strategy mapping based on stages and error codes
    if stage == 1:
        if "TIMEOUT" in error_code or "NETWORK" in error_code or "SYSTEM" in error_code:
            reason = "Temporary network timeout or provider bank downtime."
            suggested_channel = "SMS"
            wait_time = 15  # wait 15 minutes for network recovery
            message = f"Hi {customer_name}, we noticed your payment of Rs.{amount:.2f} failed due to a bank network issue. No worries! You can complete it now using this secure link: [recovery_url]"
            explanation = "Network issue detected. Waiting 15 minutes before sending SMS to allow bank networks to stabilize."
        elif "INSUFFICIENT" in error_code or "LIMIT" in error_code:
            reason = "Insufficient funds or card/UPI limits exceeded."
            suggested_channel = "WhatsApp"
            wait_time = 5
            message = f"Hey {customer_name}! Your payment of Rs.{amount:.2f} couldn't go through due to account limits or funds. Direct pay link to retry or use another payment method: [recovery_url]"
            explanation = "Funds/limits issue. Sending immediate WhatsApp with a quick-retry link to try another payment option."
        else:
            # Abandoned cart / general failure
            reason = "Customer dropped off or general payment failure."
            suggested_channel = "Email"
            wait_time = 30
            message = f"Hi {customer_name},\n\nWe noticed you couldn't complete your order of Rs.{amount:.2f}. We have saved your items for you! Click the link below to complete your checkout:\n\n[recovery_url]\n\nBest,\nMerchant Team"
            explanation = "General failure. Sending email reminder with cart recovery link in 30 minutes."
    elif stage == 2:
        reason = "First retry did not succeed. Escalating recovery."
        suggested_channel = "WhatsApp"
        wait_time = 180  # wait 3 hours
        message = f"Hey {customer_name}, we still have your cart saved. Here is your personalized checkout link to finish your order: [recovery_url]. Ping us if you face any issues!"
        explanation = "Second touchpoint. Escalating to WhatsApp for higher visibility after 3 hours."
    else:
        # Final touchpoint - offer incentive or final check
        reason = "Final attempt to recover transaction."
        suggested_channel = "SMS"
        wait_time = 1440  # wait 1 day
        if amount > 1000:
            message = f"Final call, {customer_name}! Complete your order of Rs.{amount:.2f} in the next 24 hours to get an extra 5% off. Use link: [recovery_url]"
        else:
            message = f"Hi {customer_name}, this is our last reminder to complete your order of Rs.{amount:.2f} before it expires. Click here: [recovery_url]"
        explanation = "Final touchpoint. Sending SMS after 24 hours. Offering a 5% discount if high value."
        
    return RecoveryStrategy(
        diagnosed_reason=reason,
        suggested_channel=suggested_channel,
        wait_time_minutes=wait_time,
        message_content=message,
        explanation=explanation
    )

class RecoveryAgent:
    def __init__(self):
        self.api_key = os.environ.get("GEMINI_API_KEY")
        self.model_name = "gemini-1.5-flash"
        
        if self.api_key:
            try:
                genai.configure(api_key=self.api_key)
                self.model = genai.GenerativeModel(self.model_name)
                logger.info("Gemini LLM configured successfully.")
            except Exception as e:
                logger.error(f"Failed to configure Gemini API: {e}. Falling back to rule-based.")
                self.api_key = None
        else:
            logger.info("No GEMINI_API_KEY found in environment. Using rule-based fallback.")

    def run_diagnosis(
        self, 
        db: Session, 
        pipeline: RecoveryPipeline, 
        transaction: Transaction
    ) -> RecoveryStrategy:
        """
        Diagnose the payment failure and return a recovery strategy.
        """
        # 1. Evaluate stopping rules first
        if pipeline.current_stage > pipeline.max_stages:
            logger.info(f"Pipeline {pipeline.id} hit max stages ({pipeline.max_stages}). Stopping.")
            pipeline.status = "failed_max_retries"
            db.commit()
            
            # Log stopping rule execution in audit trail
            self._log_audit(db, pipeline.id, "STOPPING_RULE_TRIGGERED", 
                            f"Pipeline stopped. Max recovery attempts ({pipeline.max_stages}) reached.")
            return None
            
        if pipeline.status == "stopped_opt_out":
            logger.info(f"Pipeline {pipeline.id} is stopped due to customer opt-out.")
            self._log_audit(db, pipeline.id, "STOPPING_RULE_TRIGGERED", 
                            "Pipeline stopped. Customer opted out of communications.")
            return None

        # Fetch previous message history for context
        history = db.query(MessageLog).filter(MessageLog.pipeline_id == pipeline.id).all()
        history_desc = ""
        if history:
            history_desc = "\nPrevious messages sent:\n" + "\n".join(
                [f"- [{h.sent_at.strftime('%Y-%m-%d %H:%M')}] {h.channel}: '{h.content}'" for h in history]
            )

        if not self.api_key:
            # Run rule-based agent
            strategy = get_rule_based_strategy(
                error_code=transaction.error_code,
                customer_name=transaction.customer_name,
                amount=transaction.amount,
                stage=pipeline.current_stage
            )
        else:
            # Run LLM-based agent
            try:
                prompt = self._build_prompt(transaction, pipeline.current_stage, history_desc)
                
                # Request structured JSON back from Gemini
                response = self.model.generate_content(
                    prompt,
                    generation_config={"response_mime_type": "application/json"}
                )
                
                plan_json = json.loads(response.text)
                strategy = RecoveryStrategy(**plan_json)
                logger.info(f"AI Agent diagnosis successful for transaction {transaction.order_id}.")
            except Exception as e:
                logger.error(f"Error calling Gemini LLM: {e}. Falling back to rule-based.")
                strategy = get_rule_based_strategy(
                    error_code=transaction.error_code,
                    customer_name=transaction.customer_name,
                    amount=transaction.amount,
                    stage=pipeline.current_stage
                )

        # Log AI reasoning in audit trail
        self._log_audit(
            db, 
            pipeline.id, 
            "AGENT_DIAGNOSIS", 
            f"AI Diagnosed: '{strategy.diagnosed_reason}'. Strategy: Send {strategy.suggested_channel} in {strategy.wait_time_minutes} min. Reasoning: {strategy.explanation}"
        )
        
        return strategy

    def _build_prompt(self, transaction: Transaction, stage: int, history_desc: str) -> str:
        return f"""
You are an expert AI Revenue Recovery Agent working for a merchant integrated with Razorpay.
Your goal is to recover a failed transaction or checkout drop-off, without being spammy.
You must return a JSON response matching this schema:
{{
  "diagnosed_reason": "Brief summary of why payment failed based on error codes",
  "suggested_channel": "SMS", "Email", or "WhatsApp",
  "wait_time_minutes": integer (delay before sending this message, e.g., 0 for instant, 15, 120, etc.),
  "message_content": "Highly personalized, friendly message to win back the user. If phone starts with '+91' or look Indian, feel free to use Hinglish (Hindi words in English letters) for SMS/WhatsApp as it converts better. Include the placeholder [recovery_url] which will be dynamically replaced later.",
  "explanation": "Why you chose this channel, wait time, and messaging style"
}}

---
TRANSACTION CONTEXT:
- Customer Name: {transaction.customer_name}
- Email: {transaction.customer_email}
- Phone: {transaction.customer_phone}
- Amount: INR {transaction.amount:.2f}
- Error Code: {transaction.error_code}
- Error Description: {transaction.error_description}
- Current Attempt Stage: {stage} of 3
{history_desc}

---
GUIDELINES:
1. Stage 1: Keep it helpful. If bank network failed, wait 10-15m and suggest trying again. If card declined/insufficient funds, offer quick options (like UPI or different card) in 5-10m.
2. Stage 2: Friendly check-in. Suggest WhatsApp.
3. Stage 3 (Final attempt): Add urgency or a tiny incentive if transaction amount is high (e.g., above ₹1,000, offer a 5% discount if they complete within 24 hours).
4. Respect boundaries. Always include a short unsubscribe phrase in messages like 'Reply STOP to opt out' or 'Unsubscribe'.
5. Always return VALID JSON matching the schema. Do not output markdown block formatting.
"""

    def _log_audit(self, db: Session, pipeline_id: int, event_type: str, description: str):
        audit = AuditLog(
            pipeline_id=pipeline_id,
            event_type=event_type,
            description=description
        )
        db.add(audit)
        db.commit()
