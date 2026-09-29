"""Phone-agent backend: receives the inbound agent's webhook tool calls and
fires ElevenLabs outbound calls.

Endpoints:
  POST /tools/request-call   <- ElevenLabs webhook tool (inbound agent)
  GET  /health
  GET  /tasks                <- recent task log (for the report-back job)

Env: ELEVENLABS_API_KEY, OUTBOUND_AGENT_ID, PHONE_NUMBER_ID,
     AARON_CALLER_NUMBER (optional verification), PORT
"""

import json
import logging
import os
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from flask import Flask, g, request, jsonify

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("phone-agent")

app = Flask(__name__)

EL_KEY = os.environ.get("ELEVENLABS_API_KEY", "")
OUTBOUND_AGENT_ID = os.environ.get("OUTBOUND_AGENT_ID", "")
PHONE_NUMBER_ID = os.environ.get("PHONE_NUMBER_ID", "")
AARON_CALLER_NUMBER = os.environ.get("AARON_CALLER_NUMBER", "")
TASK_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tasks.jsonl")
EL_OUTBOUND_URL = "https://api.elevenlabs.io/v1/convai/twilio/outbound-call"


@app.before_request
def _start_timer():
    g.start_time = time.time()


@app.after_request
def _log_request(response):
    duration_ms = int((time.time() - getattr(g, "start_time", time.time())) * 1000)
    log.info("%s %s -> %s (%dms)", request.method, request.path,
             response.status_code, duration_ms)
    return response


def log_task(entry):
    entry["logged_at"] = datetime.now(timezone.utc).isoformat()
    with open(TASK_LOG, "a") as f:
        f.write(json.dumps(entry) + "\n")


def elevenlabs_outbound(to_number, task_brief, contact_name):
    """Trigger an ElevenLabs outbound call. Returns (ok, payload)."""
    first_line = task_brief.strip().split(".")[0][:160]
    body = {
        "agent_id": OUTBOUND_AGENT_ID,
        "agent_phone_number_id": PHONE_NUMBER_ID,
        "to_number": to_number,
        "conversation_initiation_client_data": {
            "dynamic_variables": {"task_brief": task_brief}
        },
        "conversation_config_override": {
            "agent": {
                "first_message": (
                    f"Hi, this is an AI assistant calling on behalf of Aaron Langley "
                    f"regarding {contact_name} — {first_line}."
                )
            }
        },
    }
    req = urllib.request.Request(
        EL_OUTBOUND_URL,
        data=json.dumps(body).encode(),
        headers={"xi-api-key": EL_KEY, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return True, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode()[:500]
        except Exception:
            detail = ""
        log.warning("ElevenLabs outbound call failed: HTTP %s", e.code)
        return False, {"http_status": e.code, "detail": detail}
    except Exception as e:
        log.warning("ElevenLabs outbound call error: %s", str(e)[:200])
        return False, {"error": str(e)[:300]}


@app.route("/health", methods=["GET"])
def health():
    return jsonify(ok=True, time=time.time())


@app.route("/tools/request-call", methods=["POST"])
def request_call():
    data = request.get_json(force=True) or {}
    to_number = (data.get("to_number") or "").strip()
    contact_name = (data.get("contact_name") or "").strip()
    task_brief = (data.get("task_brief") or "").strip()
    caller = (data.get("caller_number") or "").strip()

    if not to_number or not task_brief:
        return jsonify(ok=False, error="to_number and task_brief are required"), 400
    if AARON_CALLER_NUMBER and caller and caller != AARON_CALLER_NUMBER:
        log_task({"event": "rejected", "reason": "caller_mismatch", "caller": caller})
        log.warning("Rejected request-call: caller mismatch")
        return jsonify(ok=False, error="caller not authorized"), 403
    if not EL_KEY or not OUTBOUND_AGENT_ID or not PHONE_NUMBER_ID:
        log.error("request-call refused: ElevenLabs config missing")
        return jsonify(ok=False, error="server missing ElevenLabs config"), 500

    ok, payload = elevenlabs_outbound(to_number, task_brief, contact_name or "this matter")
    log_task({
        "event": "outbound_call",
        "ok": ok,
        "to_number": to_number,
        "contact_name": contact_name,
        "task_brief": task_brief,
        "caller": caller,
        "response": payload,
    })
    status = 200 if ok else 502
    return jsonify(ok=ok, **({"conversation_id": payload.get("conversation_id")}
                             if ok else {"error": payload})), status


@app.route("/tasks", methods=["GET"])
def tasks():
    if not os.path.exists(TASK_LOG):
        return jsonify(tasks=[])
    with open(TASK_LOG) as f:
        lines = [json.loads(l) for l in f if l.strip()]
    return jsonify(tasks=lines[-50:])


if __name__ == "__main__":
    log.info("config: ELEVENLABS_API_KEY=%s OUTBOUND_AGENT_ID=%s "
             "PHONE_NUMBER_ID=%s AARON_CALLER_NUMBER=%s",
             bool(EL_KEY), bool(OUTBOUND_AGENT_ID),
             bool(PHONE_NUMBER_ID), bool(AARON_CALLER_NUMBER))
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
