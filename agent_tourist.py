
# agentic_tourist.py
import os
import json
import uuid
from fastapi import FastAPI, Request, BackgroundTasks
from pydantic import BaseModel
from twilio.rest import Client as TwilioClient
from twilio.twiml.voice_response import VoiceResponse, Gather, Say
import httpx

# Replace with your credentials / environment variables
TWILIO_SID = os.getenv("TWILIO_SID")
TWILIO_AUTH = os.getenv("TWILIO_AUTH")
TWILIO_FROM = os.getenv("TWILIO_FROM")  # e.g. +1xxx or +91xxx
LLM_API_KEY = os.getenv("LLM_API_KEY")  # OpenAI or your LLM key
BASE_URL = os.getenv("BASE_URL", "https://your-server.com")  # must be publicly reachable for Twilio webhooks

twilio = TwilioClient(TWILIO_SID, TWILIO_AUTH)
app = FastAPI()



@app.get("/")
def home():
    return {"msg": "Tourist Agent running!"}








# --- Simple in-memory store (replace with Redis + Postgres in prod)
CALL_STATE = {}  # call_sid -> metadata






class TriggerEvent(BaseModel):
    user_id: int
    user_name: str
    phone: str
    event_type: str
    event_payload: dict









# ---------- LLM decision function using Groq API ----------
async def llm_decide_action(event: TriggerEvent) -> dict:
    """
    Ask the LLM (Groq API) what to do. Returns a decision dict:
    { action: "call"|"notify"|"ignore", message: str, escalation: bool, max_attempts: int }
    """
    prompt = f"""
You are SafetyAgent. A tourist event occurred.
User: {event.user_name} ({event.phone})
Event type: {event.event_type}
Payload: {json.dumps(event.event_payload)}

Decide:
- action: CALL / NOTIFY / IGNORE
- message: Short message to speak when calling.
- escalation: true/false (if no or negative response escalate)
- max_attempts: number of call attempts before escalate.

Return JSON only.
"""

    headers = {
        "Authorization": f"Bearer {LLM_API_KEY}",   # Groq API key
        "Content-Type": "application/json"
    }

    body = {
        "model": "llama-3.1-8b-instant",  # Free LLaMA-3 model
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 200
    }

    async with httpx.AsyncClient() as client:
        r = await client.post(
            "https://api.groq.com/openai/v1/chat/completions",
            json=body,
            headers=headers,
            timeout=30.0
        )
        r.raise_for_status()
        text = r.json()["choices"][0]["message"]["content"]

    # Try parsing model's JSON response
    try:
        decision = json.loads(text)
    except Exception:
        decision = {
            "action": "call",
            "message": "Hello, this is Tourist Safety. We detected unusual activity. Are you safe? Press 1 for Yes, 2 for Help.",
            "escalation": True,
            "max_attempts": 2
        }
    return decision












# ---------- Twilio webhook: generate TwiML for the call ----------
@app.post("/twilio/voice")
async def twilio_voice(request: Request):
    """
    Twilio will call this to get instructions for the call.
    We'll return TwiML with Gather for DTMF and speech.
    """
    form = await request.form()
    call_sid = form.get("CallSid")
    # Look up the call state
    meta = CALL_STATE.get(call_sid, {})
    # default message
    message = meta.get("message", "We noticed unusual activity. Are you safe? Press 1 for Yes, 2 for Help.")
    vr = VoiceResponse()
    gather = Gather(input="dtmf speech", num_digits=1, timeout=6, action=f"{BASE_URL}/twilio/gather?call_sid={call_sid}", method="POST")
    gather.say(message)
    vr.append(gather)
    # fallback if no input: final message
    vr.say("We did not receive input. We will try again.")
    return Response(content=str(vr), media_type="application/xml")












# ---------- Handle gather (DTMF / speech) ----------
from fastapi.responses import Response

@app.post("/twilio/gather")
async def twilio_gather(request: Request):
    form = await request.form()
    digits = form.get("Digits")
    speech_result = form.get("SpeechResult")
    call_sid = request.query_params.get("call_sid")
    # Save response
    CALL_STATE.setdefault(call_sid, {})['response'] = {"digits": digits, "speech": speech_result, "raw": dict(form)}
    # Decide next step
    # If user pressed 1 => safe
    if digits == "1" or (speech_result and "safe" in speech_result.lower()):
        vr = VoiceResponse()
        vr.say("Glad you are safe. We have noted this. Goodbye.")
        # update DB/logs...
        return Response(content=str(vr), media_type="application/xml")
    else:
        # if pressed 2 or said "help"
        vr = VoiceResponse()
        vr.say("We have alerted your emergency contact and authorities. Help is on the way.")
        # Here, trigger escalation in background
        # call emergency contact, notify police, etc.
        # (we will POST to our /escalate in background)
        # For now, return response and then escalate
        return Response(content=str(vr), media_type="application/xml")









# ---------- Trigger event endpoint (from your detection) ----------
@app.post("/trigger_event")
async def trigger_event(event: TriggerEvent, background_tasks: BackgroundTasks):
    # 1) Ask LLM what to do
    decision = await llm_decide_action(event)
    # 2) If action says 'call', make Twilio call
    if decision.get("action","").lower() == "call":
        # create a unique call id for our state
        call_sid = str(uuid.uuid4())
        CALL_STATE[call_sid] = {
            "user_id": event.user_id,
            "phone": event.phone,
            "message": decision.get("message"),
            "decision": decision,
            "attempts": 0
        }
        # place call via Twilio
        twilio_call = twilio.calls.create(
            to=event.phone,
            from_=TWILIO_FROM,
            url=f"{BASE_URL}/twilio/voice"  # Twilio will GET this URL for TwiML
        )
        # store mapping of Twilio SID -> call_sid
        CALL_STATE[twilio_call.sid] = CALL_STATE.pop(call_sid)
        CALL_STATE[twilio_call.sid]['twilio_sid'] = twilio_call.sid
        return {"status": "calling", "twilio_sid": twilio_call.sid}
    elif decision.get("action","").lower() == "notify":
        # send an SMS or push notification to user
        twilio.messages.create(body=decision.get("message"), to=event.phone, from_=TWILIO_FROM)
        return {"status":"notified"}
    else:
        return {"status":"ignored", "decision": decision}









