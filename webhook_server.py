"""
RentMall.AI — Webhook server for Render.com

Receives a POST from Base44 when a new CustomerRequest is submitted.
Waits 60 seconds, then triggers an Emily (Intake) call via Retell AI.

Retry logic:
  - If call ends with voicemail/no-answer → retry after 1 hour
  - Max 3 attempts total (including first call)
  - No calls after 9 PM CT — deferred to 9 AM CT next morning

Deploy this to Render.com as a Web Service (free tier).
Start command: uvicorn webhook_server:app --host 0.0.0.0 --port $PORT
"""

import asyncio
import json
import os
import re
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from fastapi import FastAPI, BackgroundTasks, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware

# ─── Config ───────────────────────────────────────────────────────────────────
RETELL_API_KEY   = os.getenv("RETELL_API_KEY",   "")
INTAKE_AGENT_ID  = os.getenv("INTAKE_AGENT_ID",  "")
SPANISH_AGENT_ID = os.getenv("SPANISH_AGENT_ID", "")
FROM_NUMBER      = os.getenv("FROM_NUMBER",       "+17376773393")
DELAY_SECONDS    = int(os.getenv("DELAY_SECONDS", "60"))
WEBHOOK_SECRET   = os.getenv("WEBHOOK_SECRET",    "")

BASE44_API_KEY   = os.getenv("BASE44_API_KEY",   "98dd4a214faa4e4fba2c0807f5a4f633")
BASE44_APP_ID    = os.getenv("BASE44_APP_ID",    "6821d4c4761f3a57673ddfa7")
BASE44_URL       = f"https://app.base44.com/api/apps/{BASE44_APP_ID}/entities/CustomerRequest"

MAX_ATTEMPTS     = 3
RETRY_DELAY      = 3600   # 1 hour between retries
CALL_CUTOFF_HOUR = 21     # 9 PM CT — no calls after this
CALL_RESUME_HOUR = 9      # 9 AM CT — resume next morning
CENTRAL_TZ       = ZoneInfo("America/Chicago")

# Retell disconnection reasons that warrant a retry
RETRY_REASONS = {"voicemail", "dial_no_answer", "machine_detected"}

WEBHOOK_URL = "https://rentmall-intake.onrender.com/webhook/call-ended"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

app = FastAPI(title="RentMall Intake Webhook")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

# In-memory call tracker: call_id -> metadata
pending_calls: dict = {}

# ─── Health check ─────────────────────────────────────────────────────────────
@app.get("/")
def health():
    return {"status": "ok", "service": "RentMall Intake Webhook"}


# ─── New form submission ───────────────────────────────────────────────────────
@app.post("/webhook/new-request")
async def handle_new_request(request: Request, background_tasks: BackgroundTasks):
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

    background_tasks.add_task(call_after_delay, data, phone, name, language)

    return {"status": "queued", "name": name, "phone": phone, "delay_seconds": DELAY_SECONDS}


# ─── Inbound call tool: Emily submits rental request ──────────────────────────
@app.post("/webhook/inbound-submit")
async def handle_inbound_submit(request: Request):
    """
    Called by Retell when Emily (inbound) invokes the submit_rental_request tool.
    Creates a CustomerRequest record in Base44.
    """
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    call_id   = data.get("call_id", "")
    tool_name = data.get("name", "")
    raw_args  = data.get("arguments", "{}")

    # Retell sends arguments as a JSON string
    if isinstance(raw_args, str):
        try:
            args = json.loads(raw_args)
        except Exception:
            args = {}
    else:
        args = raw_args

    log.info(f"Inbound submit from call {call_id}: {args}")

    # Build scaffolding_measurements if relevant
    scaffolding_measurements = None
    if args.get("equipment") == "scaffolding" and args.get("scaffolding_total_area"):
        total_area = args["scaffolding_total_area"]
        scaffolding_measurements = {
            "walls": [{"id": 1, "width": total_area, "height": 1, "area": total_area}],
            "total_area": total_area,
        }

    # Map load capacity description → Base44 enum
    load_map = {
        "light": "light", "workers": "light", "tools": "light",
        "medium": "medium",
        "heavy": "heavy", "materials": "heavy",
    }
    raw_load = (args.get("scaffolding_load_capacity") or "").lower()
    load_capacity = next((v for k, v in load_map.items() if k in raw_load), raw_load or None)

    phone = to_e164(args.get("phone", ""))

    # Fallback defaults for required Base44 fields
    today     = datetime.now(tz=CENTRAL_TZ)
    email     = args.get("email") or f"inbound+{(phone or 'unknown').replace('+','')}@rentmall.ai"
    start_date = args.get("start_date") or (today + timedelta(days=7)).strftime("%Y-%m-%d")
    end_date   = args.get("end_date")   or (today + timedelta(days=14)).strftime("%Y-%m-%d")

    payload = {
        "full_name":   args.get("full_name") or "Inbound caller",
        "email":       email,
        "phone":       phone or args.get("phone", ""),
        "equipment":   args.get("equipment", ""),
        "location":    args.get("location", ""),
        "start_date":  start_date,
        "end_date":    end_date,
        "status":      "pending",
        "notes":       args.get("notes", "Submitted via inbound call"),
        "delivery_option": args.get("delivery_option", "delivery"),
        "language":    "en",
        # Scaffolding
        "scaffolding_measurements": scaffolding_measurements,
        "scaffolding_load_capacity": load_capacity,
        "terrain_access": args.get("terrain_access"),
        # Scissor lift
        "scissors_working_height":           args.get("scissors_working_height"),
        "scissors_platform_weight_capacity": args.get("scissors_platform_weight_capacity"),
        "scissors_narrow_passage_width":     args.get("scissors_narrow_passage_width"),
        # Boom lift
        "boom_max_working_height":  args.get("boom_max_working_height"),
        "boom_horizontal_outreach": args.get("boom_horizontal_outreach"),
        "boom_type":                args.get("boom_type"),
        # Boom truck
        "boom_truck_load_type":         args.get("boom_truck_load_type"),
        "boom_truck_working_height":    args.get("boom_truck_working_height"),
        "boom_truck_ground_conditions": args.get("boom_truck_ground_conditions"),
    }

    # Remove None values so Base44 doesn't reject optional fields
    payload = {k: v for k, v in payload.items() if v is not None}

    try:
        resp = requests.post(
            BASE44_URL,
            headers={"api_key": BASE44_API_KEY, "Content-Type": "application/json"},
            json=payload,
            timeout=10,
        )
        if resp.ok:
            record_id = resp.json().get("id", "?")
            log.info(f"✓ Base44 record created: {record_id} | {payload.get('full_name')} | {payload.get('equipment')}")
            return {"result": "Request submitted successfully! Our team will be in touch very soon."}
        else:
            log.error(f"Base44 error {resp.status_code}: {resp.text}")
            return {"result": "Request received — our team will follow up shortly."}
    except Exception as e:
        log.error(f"Failed to create Base44 record: {e}")
        return {"result": "Request received — our team will follow up shortly."}


# ─── Retell call-ended webhook ─────────────────────────────────────────────────
@app.post("/webhook/call-ended")
async def handle_call_ended(request: Request):
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    event = data.get("event", "")
    if event != "call_ended":
        return {"status": "ignored", "event": event}

    call    = data.get("call", {})
    call_id = call.get("call_id", "")
    reason  = call.get("disconnection_reason", "")

    log.info(f"Call ended: {call_id} | reason: {reason}")

    if reason in RETRY_REASONS and call_id in pending_calls:
        meta    = pending_calls.pop(call_id)
        attempt = meta["attempt"]
        if attempt < MAX_ATTEMPTS:
            log.info(f"Will retry {meta['name']} ({meta['phone']}) — attempt {attempt + 1}/{MAX_ATTEMPTS}")
            asyncio.create_task(retry_after(meta, attempt + 1))
        else:
            log.info(f"Max attempts ({MAX_ATTEMPTS}) reached for {meta['name']} ({meta['phone']}) — stopping")

    # ── If call was successful, trigger supplier pipeline ─────────────────────
    elif call_id in pending_calls:
        meta = pending_calls.pop(call_id)
        customer_data = meta.get("data", {})
        equipment = customer_data.get("equipment", "")
        if equipment:
            log.info(f"Triggering supplier pipeline for '{equipment}' — {meta.get('name')}")
            asyncio.create_task(_run_supplier_pipeline(customer_data))

    return {"status": "ok"}


async def _run_supplier_pipeline(customer_data: dict):
    """Run supplier pipeline in background after successful customer call."""
    try:
        from supplier_pipeline import run_pipeline
        customer = {
            "equipment":   customer_data.get("equipment", ""),
            "location":    customer_data.get("location", "Houston, TX"),
            "job_address": customer_data.get("location", "Houston, TX"),
            "start_date":  customer_data.get("start_date", "soon"),
            "end_date":    customer_data.get("end_date", ""),
            "details":     customer_data.get("notes", ""),
        }
        await asyncio.to_thread(run_pipeline, customer)
    except Exception as e:
        log.error(f"Supplier pipeline error: {e}")


# ─── Background tasks ──────────────────────────────────────────────────────────
async def call_after_delay(data: dict, phone: str, name: str, language: str = "en"):
    log.info(f"Waiting {DELAY_SECONDS}s before calling {name} ({phone})...")
    await asyncio.sleep(DELAY_SECONDS)
    try:
        place_intake_call(data, phone, name, language, attempt=1)
    except Exception as e:
        log.error(f"Failed to place call to {name} ({phone}): {e}")


async def retry_after(meta: dict, attempt: int):
    now_ct   = datetime.now(tz=CENTRAL_TZ)
    next_ct  = now_ct + timedelta(seconds=RETRY_DELAY)

    if next_ct.hour >= CALL_CUTOFF_HOUR:
        # Push to 9 AM next morning
        next_morning = (now_ct + timedelta(days=1)).replace(
            hour=CALL_RESUME_HOUR, minute=0, second=0, microsecond=0
        )
        delay = (next_morning - now_ct).total_seconds()
        log.info(f"After 9 PM CT — retry #{attempt} for {meta['name']} deferred to 9 AM CT ({delay:.0f}s from now)")
    else:
        delay = RETRY_DELAY

    await asyncio.sleep(delay)
    try:
        place_intake_call(meta["data"], meta["phone"], meta["name"], meta["language"], attempt=attempt)
    except Exception as e:
        log.error(f"Retry #{attempt} failed for {meta['name']} ({meta['phone']}): {e}")


# ─── Core call function ────────────────────────────────────────────────────────
def place_intake_call(data: dict, phone: str, name: str, language: str = "en", attempt: int = 1):
    use_spanish = language.startswith("es") and bool(SPANISH_AGENT_ID)
    agent_id = SPANISH_AGENT_ID if use_spanish else INTAKE_AGENT_ID

    if not agent_id:
        log.error("Agent ID is not set")
        return

    log.info(f"Using {'Paulina (ES)' if use_spanish else 'Emily (EN)'} for {name} | attempt {attempt}/{MAX_ATTEMPTS}")

    first_name = (data.get("full_name", "") or "").strip().split()[0].capitalize() or "there"
    equipment  = (data.get("equipment", "scaffolding") or "scaffolding").replace("_", " ")
    city       = (data.get("location", "") or "")[:40]
    start_date = data.get("start_date", "") or "soon"
    end_date   = data.get("end_date", "")   or ""

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
            "webhook_url":                  WEBHOOK_URL,
        },
        timeout=15,
    )

    if resp.ok:
        call_id = resp.json().get("call_id", "?")
        log.info(f"✓ Call placed | call_id: {call_id} | attempt {attempt}/{MAX_ATTEMPTS}")
        pending_calls[call_id] = {
            "data":     data,
            "phone":    phone,
            "name":     name,
            "language": language,
            "attempt":  attempt,
        }
    else:
        log.error(f"Retell API error {resp.status_code}: {resp.text}")
        resp.raise_for_status()


# ─── Keep-alive (prevents Render free-tier sleep after 15 min inactivity) ─────
SELF_URL = os.getenv("RENDER_EXTERNAL_URL", "https://rentmall-intake.onrender.com")

async def _keep_alive_loop():
    await asyncio.sleep(60)          # wait 1 min after startup
    while True:
        try:
            await asyncio.to_thread(requests.get, f"{SELF_URL}/", timeout=10)
            log.info("keep-alive ping OK")
        except Exception as e:
            log.warning(f"keep-alive ping failed: {e}")
        await asyncio.sleep(840)     # ping every 14 minutes

@app.on_event("startup")
async def _startup():
    asyncio.create_task(_keep_alive_loop())


# ─── Helpers ──────────────────────────────────────────────────────────────────
def to_e164(phone: str) -> str:
    digits = re.sub(r"\D", "", str(phone))
    if len(digits) == 10:                      return f"+1{digits}"
    if len(digits) == 11 and digits[0] == "1": return f"+{digits}"
    return ""
