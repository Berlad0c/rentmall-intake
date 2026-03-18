"""
RentMall.AI — Setup: Inbound call agent (Emily) for Retell AI.

This creates a new agent that handles INCOMING calls to +17376773393.
When a customer calls, Emily collects their rental request details
and submits them to Base44 via a custom tool.

Run ONCE:
    python setup_inbound_agent.py
"""

import requests
import json

API_KEY = "key_d6167a8f532f9f5dd9cf47a53693"
HEADERS = {
    "Authorization": f"Bearer {API_KEY}",
    "Content-Type":  "application/json",
}

PHONE_NUMBER    = "+17376773393"
WEBHOOK_SUBMIT  = "https://rentmall-intake.onrender.com/webhook/inbound-submit"

# ─── Agent Prompt ─────────────────────────────────────────────────────────────
PROMPT = """You are Emily, a friendly equipment rental coordinator at RentMall.AI.

When someone calls, your job is to understand their equipment rental need, collect all the required details, and submit their request so our team can find them the best deal fast.

Be warm, natural, and efficient. Keep your speaking turns short. Acknowledge what they say before asking the next question.

───────────────────────────────────────────────────────────────────────────────
OPENING
───────────────────────────────────────────────────────────────────────────────

"Hi! I'm Emily, customer service at RentMall AI — we're an AI-powered service for construction equipment rentals. I'll get your details in just a moment and handle the whole process for you. What equipment are you looking for?"

[Listen for their equipment type and acknowledge it warmly before continuing.]

───────────────────────────────────────────────────────────────────────────────
STEP 2 — Equipment-Specific Questions
───────────────────────────────────────────────────────────────────────────────

Ask only the questions relevant to the equipment type. Keep it conversational.

SCAFFOLDING:
1. "What's the total square footage you need to cover? A rough estimate is fine — we can always adjust."
2. "What's the maximum height you'll need?"
3. "What kind of surface will it be on — solid concrete or asphalt, or softer ground?"
4. "And what kind of load — mostly workers and light tools, or heavier materials?"

SCISSOR LIFT:
1. "What's the maximum working height you need to reach?"
2. "How much weight does it need to carry?"
3. "Will it need to fit through any narrow passages? If so, roughly how wide?"

BOOM LIFT:
1. "What's the maximum working height you need?"
2. "Do you need it to reach out horizontally as well, or mostly straight up?"
3. "Any preference on type — articulating, telescopic, or are you open to our recommendation?"

BOOM TRUCK:
1. "Is this for lifting personnel, materials, or both?"
2. "What's the maximum height you need to reach?"
3. "What's the ground condition at the site — solid surface, or softer ground?"

ANY OTHER EQUIPMENT:
1. "Can you describe the job? What will you be using it for?"
2. Ask 1–2 natural follow-up questions based on their answer.

───────────────────────────────────────────────────────────────────────────────
STEP 3 — Universal Details
───────────────────────────────────────────────────────────────────────────────

After equipment questions, collect:

1. "What city or address is the job site?"
2. "When do you need it by?"
3. "And how long do you need it — do you have an end date in mind?"
4. "Do you need it delivered to the site, or can you pick it up?"

───────────────────────────────────────────────────────────────────────────────
STEP 4 — Contact Info
───────────────────────────────────────────────────────────────────────────────

1. "Can I get your name?"
2. "And a good email address to send the quote to?"

───────────────────────────────────────────────────────────────────────────────
STEP 5 — Confirm & Submit
───────────────────────────────────────────────────────────────────────────────

Summarize what you collected:
"Alright — let me just confirm: you need [equipment] at [location], from [start_date] to [end_date]. [Any key specs.] Does that sound right?"

[If they confirm: call submit_rental_request with all the data you collected.]
[If they correct something: update and re-confirm before submitting.]

After calling submit_rental_request:
"You're all set! Our team is already working on finding you the best options. You'll hear from us very soon — usually within the hour. Is there anything else I can help you with?"

[Then end the call.]

───────────────────────────────────────────────────────────────────────────────
RULES
───────────────────────────────────────────────────────────────────────────────

- NEVER give price quotes. Say: "Our team will put together the best options and pricing for you."
- If they ask how long: "Usually within the hour."
- Keep your turns short — one question at a time, then stop and listen.
- Sound warm and human. You're helping them, not interrogating them.
- If they're in a hurry, move faster. The minimum required: equipment type, location, start date.
- If they ask what RentMall is: "We help contractors find and book equipment rentals fast — we handle the sourcing so you don't have to."
- Always call submit_rental_request before ending the call."""


def create_llm():
    print("Creating LLM...")
    resp = requests.post(
        "https://api.retellai.com/create-retell-llm",
        headers=HEADERS,
        json={
            "model":          "gpt-4.1",
            "general_prompt": PROMPT,
            "general_tools": [
                {
                    "type":        "end_call",
                    "name":        "end_call",
                    "description": "End the call after the request has been submitted and the conversation is complete.",
                },
                {
                    "type":        "custom",
                    "name":        "submit_rental_request",
                    "description": "Submit the customer's rental request to the RentMall system. Call this ONLY after you have confirmed all the details with the customer.",
                    "url":         WEBHOOK_SUBMIT,
                    "timeout_ms":  10000,
                    "speak_during_execution": True,
                    "speak_after_execution":  False,
                    "execution_message_description": "Say: 'Perfect — I'm submitting your request right now!'",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "full_name":    {"type": "string", "description": "Customer's full name"},
                            "email":        {"type": "string", "description": "Customer's email address"},
                            "equipment":    {"type": "string", "description": "Equipment type: scaffolding, scissors_lift, boom_lift, boom_truck, or describe it"},
                            "location":     {"type": "string", "description": "Job site city or full address"},
                            "start_date":   {"type": "string", "description": "Rental start date, YYYY-MM-DD format"},
                            "end_date":     {"type": "string", "description": "Rental end date, YYYY-MM-DD format"},
                            "delivery_option": {"type": "string", "description": "delivery or pickup"},
                            "notes":        {"type": "string", "description": "All equipment-specific details and any extra notes from the conversation"},
                            # Scaffolding
                            "scaffolding_total_area":    {"type": "number", "description": "Total square footage (scaffolding)"},
                            "scaffolding_load_capacity": {"type": "string", "description": "light, medium, or heavy (scaffolding)"},
                            "terrain_access":            {"type": "string", "description": "stable_surface or uncompacted_ground"},
                            # Scissor lift
                            "scissors_working_height":          {"type": "number", "description": "Max working height in feet"},
                            "scissors_platform_weight_capacity": {"type": "number", "description": "Weight capacity in lbs"},
                            "scissors_narrow_passage_width":     {"type": "number", "description": "Min passage width in inches"},
                            # Boom lift
                            "boom_max_working_height":  {"type": "number", "description": "Max working height in feet"},
                            "boom_horizontal_outreach": {"type": "number", "description": "Horizontal outreach in feet"},
                            "boom_type":                {"type": "string", "description": "articulating, telescopic, or spider_boom"},
                            # Boom truck
                            "boom_truck_load_type":        {"type": "string", "description": "personnel, materials, or other"},
                            "boom_truck_working_height":   {"type": "number", "description": "Max working height in feet"},
                            "boom_truck_ground_conditions":{"type": "string", "description": "stable_surface or uncompacted_ground"},
                        },
                        "required": ["equipment", "location", "start_date"],
                    },
                },
            ],
            "begin_after_user_silence_ms": 1500,
        },
    )
    resp.raise_for_status()
    llm_id = resp.json()["llm_id"]
    print(f"  LLM created: {llm_id}")
    return llm_id


def create_agent(llm_id):
    print("Creating agent...")
    resp = requests.post(
        "https://api.retellai.com/create-agent",
        headers=HEADERS,
        json={
            "agent_name":               "Inbound — RentMall.AI",
            "response_engine":          {"type": "retell-llm", "llm_id": llm_id},
            "voice_id":                 "11labs-Emily",
            "language":                 "en-US",
            "enable_backchannel":       True,
            "backchannel_frequency":    0.1,
            "interruption_sensitivity": 0.85,
            "responsiveness":           0.9,
            "post_call_analysis_model": "gpt-4.1-mini",
            "data_storage_setting":     "everything",
        },
    )
    resp.raise_for_status()
    agent_id = resp.json()["agent_id"]
    print(f"  Agent created: {agent_id}")
    return agent_id


def assign_inbound(agent_id):
    print(f"Assigning inbound agent to {PHONE_NUMBER}...")
    resp = requests.patch(
        f"https://api.retellai.com/v2/phone-number/{PHONE_NUMBER}",
        headers=HEADERS,
        json={"inbound_agent_id": agent_id},
    )
    if resp.ok:
        print(f"  ✓ {PHONE_NUMBER} now routes inbound calls to Emily")
    else:
        print(f"  ✗ Failed to assign inbound: {resp.status_code} {resp.text}")


def main():
    print("\n=== RentMall.AI — Inbound Agent Setup ===\n")
    llm_id   = create_llm()
    agent_id = create_agent(llm_id)
    assign_inbound(agent_id)
    print(f"\n✓ Done! INBOUND_AGENT_ID: {agent_id}")
    print(f"  Phone {PHONE_NUMBER} now handles inbound calls.")
    print(f"  Make sure Render has BASE44_API_KEY set.")


if __name__ == "__main__":
    main()
