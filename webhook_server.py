"""
RentMall.AI — Webhook server for Render.com

Receives a POST from Base44 when a new CustomerRequest is submitted.
Waits 60 seconds, then triggers an Emily (Intake) call via Retell AI.

Deploy this to Render.com as a Web Service (free tier).
Start command: uvicorn webhook_server:app --host 0.0.0.0 --port $PORT
"""

import asyncio
import json
import os
import re
import logging
from datetime import datetime

import requests
from fastapi import FastAPI, BackgroundTasks, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware

# ─── Config ───────────────────────────────────────────────────────────────────
# These can also be set as environment variables on Render.com for security.
RETELL_API_KEY   = os.getenv("RETELL_API_KEY",   "")
INTAKE_AGENT_ID  = os.getenv("INTAKE_AGENT_ID",  "")
SPANISH_AGENT_ID = os.getenv("SPANISH_AGENT_ID", "")
FROM_NUMBER      = os.getenv("FROM_NUMBER",       "+17376773393")
DELAY_SECONDS    = int(os.getenv("DELAY_SECONDS", "60"))
WEBHOOK_SECRET   = os.getenv("WEBHOOK_SECRET",    "")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

app = FastAPI(title="RentMall Intake Webhook")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

# ─── Health check ─────────────────────────────────────────────────────────────
@app.get("/")
def health():
    return {"status": "ok", "service": "RentMall Intake Webhook"}

# ─── Main webhook endpoint ────────────────────────────────────────────────────
@app.post("/webhook/new-request")
async def handle_new_request(request: Request, background_tasks: BackgroundTasks):
    """
    Called by Base44 when a new CustomerRequest is created.
    Expected payload fields:
      phone, full_name, equipment, location,
      start_date, end_date, scaffolding_measurements (optional JSON)
    """
    # Optional: verify shared secret header
    if WEBHOOK_SECRET:
        secret = request.headers.get("X-Webhook-Secret", "")
        if secret != WEBHOOK_SECRET:
            raise HTTPException(status_code=403, detail="Invalid secret")

    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    phone    = to_e164(data.get("phone", ""))
    name     = data.get("full_name", "unknown")
    language = data.get("language", "en")

    if not phone:
        log.warning(f"Received request with invalid phone for '{name}' — skipping")
        return {"status": "skipped", "reason": "invalid_phone"}

    log.info(f"New request received: {name} | {phone} | {data.get('equipment')} | {data.get('location')}")

    # Queue the call in the background (non-blocking)
    background_tasks.add_task(call_after_delay, data, phone, name, language)

    return {"status": "queued", "name": name, "phone": phone, "delay_seconds": DELAY_SECONDS}


# ─── Background task ──────────────────────────────────────────────────────────
async def call_after_delay(data: dict, phone: str, name: str, language: str = "en"):
    log.info(f"Waiting {DELAY_SECONDS}s before calling {name} ({phone})...")
    await asyncio.sleep(DELAY_SECONDS)
    try:
        place_intake_call(data, phone, name, language)
    except Exception as e:
        log.error(f"Failed to place call to {name} ({phone}): {e}")


def place_intake_call(data: dict, phone: str, name: str, language: str = "en"):
    use_spanish = language.startswith("es") and bool(SPANISH_AGENT_ID)
    agent_id = SPANISH_AGENT_ID if use_spanish else INTAKE_AGENT_ID

    if not agent_id:
        log.error("Agent ID is not set")
        return

    log.info(f"Using {'Paulina (ES)' if use_spanish else 'Emily (EN)'} for {name}")

    first_name = (data.get("full_name", "") or "").strip().split()[0].capitalize() or "there"
    equipment  = (data.get("equipment", "scaffolding") or "scaffolding").replace("_", " ")
    city       = (data.get("location", "") or "")[:40]
    start_date = data.get("start_date", "") or "soon"
    end_date   = data.get("end_date", "")   or ""

    # Extract total_area from scaffolding_measurements JSON
    measurements = data.get("scaffolding_measurements") or {}
    if isinstance(measurements, str):
        try:
            measurements = json.loads(measurements)
        except Exception:
            measurements = {}
    total_area = str(measurements.get("total_area", "")) if measurements else ""

    dynamic_vars = {
        "customer_first_name": first_name,
        "equipment":           equipment,
        "city":                city,
        "start_date":          start_date,
        "end_date":            end_date,
        "total_area":          total_area,
    }

    log.info(f"Placing call → {name} ({phone}) | vars: {dynamic_vars}")

    resp = requests.post(
        "https://api.retellai.com/v2/create-phone-call",
        headers={
            "Authorization": f"Bearer {RETELL_API_KEY}",
            "Content-Type":  "application/json",
        },
        json={
            "from_number":                  FROM_NUMBER,
            "to_number":                    phone,
            "override_agent_id":            agent_id,
            "retell_llm_dynamic_variables": dynamic_vars,
        },
        timeout=15,
    )

    if resp.ok:
        call_id = resp.json().get("call_id", "?")
        log.info(f"✓ Call placed successfully | call_id: {call_id}")
    else:
        log.error(f"Retell API error {resp.status_code}: {resp.text}")
        resp.raise_for_status()


# ─── Helpers ──────────────────────────────────────────────────────────────────
def to_e164(phone: str) -> str:
    digits = re.sub(r"\D", "", str(phone))
    if len(digits) == 10:                      return f"+1{digits}"
    if len(digits) == 11 and digits[0] == "1": return f"+{digits}"
    return ""
