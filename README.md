# Razorpay AI Revenue Recovery Agent (Track 3)

This is an autonomous AI agent system designed to intercept failed payments, checkout drop-offs, and subscription failures on Razorpay, diagnose the root cause of failures, execute bounded recovery sequences (via personalized email/SMS alerts), and track recovery metrics and audit trails.

## Features

1. **Dashboard & Web UI:** Real-time statistics on total leaked vs. recovered revenue, active pipelines, and recovery rate.
2. **AI Diagnosis Engine:** Leverages LLM capabilities (with Gemini/fallback) to write customized Hinglish/English communications matching user context.
3. **Audit Trail & Logging:** Chronological events log of webhook receipts, agent thinking, notifications dispatched, and user interactions.
4. **Compliance & Stopping Rules:** Ensures no customer spamming (stops after 3 attempts or immediately on customer opt-out `STOP`).
5. **Interactive Simulator:** Allows triggering individual failure cases or simulated batches (e.g. 20 or 50) and simulating customer actions (clicks pay link, opts out).

## Getting Started

### Prerequisites

- Python 3.10+ (tested on Python 3.14)
- Pip

### Setup

1. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

2. **Configure Environment Variables (Optional):**
   To enable AI-based messaging, set your Gemini API key:
   ```bash
   # Windows (PowerShell)
   $env:GEMINI_API_KEY="your_api_key_here"
   
   # Windows (CMD)
   set GEMINI_API_KEY=your_api_key_here
   ```
   *Note: If no API key is supplied, the agent automatically falls back to a rule-based simulation engine.*

### Running the App

1. Start the FastAPI development server:
   ```bash
   python -m uvicorn app.main:app --reload
   ```

2. Open your web browser and navigate to:
   [http://localhost:8000](http://localhost:8000)

## Project Layout

- `app/`
  - `database.py`: SQLite SQLAlchemy schema and connection config.
  - `agent.py`: AI Agent diagnosis logic and stopping rules.
  - `simulator.py`: Webhook and customer interaction simulators.
  - `main.py`: FastAPI routes, webhook receiver, and data serialization.
- `templates/`
  - `index.html`: Dashboard built with Tailwind CSS & JavaScript.
- `tests/`
  - `test_agent.py`: Unit tests for agent logic, stopping rules, and opt-outs.
  - `test_recovery.py`: Integration tests for full pipeline execution.

## Running Tests

Run the test suite with pytest:
```bash
pytest
```
