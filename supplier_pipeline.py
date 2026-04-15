"""
RentMall.AI — Supplier Pipeline
================================
Reads approved_vendors.json, matches suppliers to a customer request,
and contacts each matching supplier via SMS or Call.

Called automatically from webhook_server.py after Emily's verification call.
"""

import json, logging, os, re, requests, time
from datetime import datetime
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ─── Config (from env vars on Render) ────────────────────────────────────────
RETELL_API_KEY    = os.getenv("RETELL_API_KEY", "")
SUPPLIER_AGENT_ID = os.getenv("SUPPLIER_AGENT_ID", "")

CALL_FROM    = "+12057086353"   # (205) 708-6353 — dedicated supplier line
SMS_FROM     = "+12057086353"
VENDORS_PATH = Path(__file__).parent / "approved_vendors.json"
LOG_PATH     = Path(__file__).parent / "supplier_pipeline_log.json"


# ─── Load approved vendors ────────────────────────────────────────────────────
def load_vendors():
    return json.loads(VENDORS_PATH.read_text(encoding="utf-8"))

def matches_equipment(vendor, equipment: str) -> bool:
    equip = equipment.lower().strip().replace("_", " ")
    return any(equip in s.lower() or s.lower() in equip
               for s in vendor.get("specialization", []))


# ─── Phone helper ─────────────────────────────────────────────────────────────
def to_e164(contact_str: str):
    digits = re.sub(r"\D", "", str(contact_str))
    if len(digits) >= 11 and digits[0] == "1":
        return f"+{digits[:11]}"
    if len(digits) == 10:
        return f"+1{digits}"
    return None


# ─── Contact methods ──────────────────────────────────────────────────────────
def send_sms(phone: str, customer: dict) -> bool:
    body = (
        f"Hi! I'm Emily, assistant to Jay Cohen (general contractor). "
        f"We have a client needing {customer['equipment']} in {customer['location']}, "
        f"starting {customer['start_date']} through {customer['end_date']}. "
        f"Can you send a quick quote? Reach Jay at (737) 414-6845. Thanks!"
    )
    resp = requests.post(
        "https://api.retellai.com/v2/send-sms",
        headers={
            "Authorization": f"Bearer {RETELL_API_KEY}",
            "Content-Type":  "application/json",
        },
        json={"from_number": SMS_FROM, "to_number": phone, "content": body},
        timeout=15,
    )
    if resp.ok:
        log.info(f"  SMS sent to {phone}")
        return True
    else:
        log.warning(f"  SMS failed {phone}: {resp.status_code} {resp.text[:100]}")
        return False


def place_call(phone: str, customer: dict):
    resp = requests.post(
        "https://api.retellai.com/v2/create-phone-call",
        headers={
            "Authorization": f"Bearer {RETELL_API_KEY}",
            "Content-Type":  "application/json",
        },
        json={
            "from_number":       CALL_FROM,
            "to_number":         phone,
            "override_agent_id": SUPPLIER_AGENT_ID,
            "retell_llm_dynamic_variables": {
                "equipment":   customer["equipment"],
                "location":    customer.get("location", "Houston, TX"),
                "job_address": customer.get("job_address", "Houston, TX"),
                "start_date":  customer["start_date"],
                "end_date":    customer["end_date"],
                "details":     customer.get("details", ""),
            },
        },
        timeout=15,
    )
    if resp.ok:
        call_id = resp.json().get("call_id", "?")
        log.info(f"  Call placed to {phone} — call_id: {call_id}")
        return call_id
    else:
        log.warning(f"  Call failed {phone}: {resp.status_code} {resp.text[:100]}")
        return None


# ─── Core pipeline ────────────────────────────────────────────────────────────
def run_pipeline(customer: dict) -> list:
    """
    Main entry. customer dict must have: equipment, location, start_date, end_date
    Optional: job_address, details
    """
    equipment = customer["equipment"].lower().strip()
    log.info(f"Pipeline start — {equipment} | {customer.get('location')}")

    vendors = load_vendors()
    matched = [v for v in vendors if matches_equipment(v, equipment)]
    log.info(f"Vendors: {len(vendors)} total | {len(matched)} matched for '{equipment}'")

    if not matched:
        log.warning("No approved vendors match this equipment type.")
        return []

    results = []
    for v in matched:
        phone = to_e164(v["contact"])
        if not phone:
            log.warning(f"  {v['name']} — invalid contact: '{v['contact']}'")
            results.append({"vendor": v["name"], "status": "skipped", "reason": "no_phone"})
            continue

        approach = v.get("approach", "call")
        log.info(f"  {v['name']} | {approach} | {phone}")

        if approach == "sms":
            ok = send_sms(phone, customer)
            results.append({
                "vendor": v["name"], "phone": phone, "approach": "sms",
                "status": "sent" if ok else "failed",
                "time": datetime.now().isoformat(),
            })
        else:
            call_id = place_call(phone, customer)
            results.append({
                "vendor": v["name"], "phone": phone, "approach": "call",
                "status": "placed" if call_id else "failed",
                "call_id": call_id,
                "time": datetime.now().isoformat(),
            })
            time.sleep(2)

    # Save log
    existing = json.loads(LOG_PATH.read_text(encoding="utf-8")) if LOG_PATH.exists() else []
    existing.append({"customer": customer, "results": results, "timestamp": datetime.now().isoformat()})
    LOG_PATH.write_text(json.dumps(existing, indent=2, ensure_ascii=False), encoding="utf-8")

    sent = sum(1 for r in results if r["status"] in ("sent", "placed"))
    log.info(f"Pipeline done — {sent}/{len(matched)} vendors contacted.")
    return results
