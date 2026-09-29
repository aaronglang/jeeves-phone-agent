"""Phone-agent backend: receives the inbound agent's webhook tool calls and
fires ElevenLabs outbound calls. Also relays non-call tasks from the
inbound agent to Aaron's personal assistant via a polled inbox.

Endpoints:
  POST /tools/request-call   <- ElevenLabs webhook tool (inbound agent).
                               caller_number is REQUIRED and must match
                               AARON_CALLER_NUMBER.
  POST /tools/relay-task     <- ElevenLabs webhook tool: hand a non-call
                               task to the assistant. Same caller check.
  GET  /health               <- public liveness check (keep-warm pings this)
  GET  /tasks                <- recent task log (bearer token required)
  GET  /inbox                <- unacknowledged relayed tasks (bearer token)
  POST /inbox/ack            <- acknowledge a relayed task (bearer token)

Env: ELEVENLABS_API_KEY, OUTBOUND_AGENT_ID, PHONE_NUMBER_ID,
     AARON_CALLER_NUMBER (required for authorization),
     INBOX_TOKEN (bearer token for /tasks, /inbox, /inbox/ack), PORT

Note: Render's filesystem is ephemeral, so the inbox is best-effort
durable: it is written through to inbox.json on every change and reloaded
on startup, but a restart between poll cycles could lose an unacked item.
The assistant polls /inbox every few minutes, so the exposure window is
small. Relayed tasks are transient by nature; nothing critical should
depend on them surviving a redeploy.
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
INBOX_TOKEN = os.environ.get("INBOX_TOKEN", "")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TASK_LOG = os.path.join(BASE_DIR, "tasks.jsonl")
INBOX_FILE = os.path.join(BASE_DIR, "inbox.json")
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


def load_inbox():
    try:
        with open(INBOX_FILE) as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_inbox(items):
    tmp = INBOX_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(items, f)
    os.replace(tmp, INBOX_FILE)


inbox_items = load_inbox()


def check_caller(caller):
    """Fail-closed caller authorization. Returns an error response or None."""
    if not AARON_CALLER_NUMBER:
        log.error("refused: AARON_CALLER_NUMBER not configured")
        return jsonify(ok=False, error="server not configured"), 500
    if not caller:
        return jsonify(ok=False, error="caller_number is required"), 400
    if caller != AARON_CALLER_NUMBER:
        log.warning("Rejected: caller mismatch")
        return jsonify(ok=False, error="caller not authorized"), 403
    return None


def require_bearer():
    """Token auth for the assistant-facing read endpoints."""
    if not INBOX_TOKEN:
        log.error("refused: INBOX_TOKEN not configured")
        return jsonify(ok=False, error="server not configured"), 500
    if request.headers.get("Authorization", "") != "Bearer " + INBOX_TOKEN:
        return jsonify(ok=False, error="unauthorized"), 401
    return None


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

    denied = check_caller(caller)
    if denied:
        if denied[1] == 403:
            log_task({"event": "rejected", "reason": "caller_mismatch",
                      "caller": caller})
        return denied
    if not to_number or not task_brief:
        return jsonify(ok=False, error="to_number and task_brief are required"), 400
    if not EL_KEY or not OUTBOUND_AGENT_ID or not OUTBOUND_AGENT_ID or not PHONE_NUMBER_ID:
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


@app.route("/tools/relay-task", methods=["POST"])
def relay_task():
    """Hand a non-call task from the inbound agent to the assistant's inbox."""
    data = request.get_json(force=True) or {}
    caller = (data.get("caller_number") or "").strip()
    task_text = (data.get("task") or "").strip()
    callback_note = (data.get("callback_note") or "").strip()

    denied = check_caller(caller)
    if denied:
        return denied
    if not task_text:
        return jsonify(ok=False, error="task is required"), 400

    now = datetime.now(timezone.utc)
    item_id = "inbox-" + now.strftime("%Y%m%d%H%M%S") + "-" + os.urandom(3).hex()
    inbox_items[item_id] = {
        "id": item_id,
        "task": task_text,
        "callback_note": callback_note,
        "caller": caller,
        "received_at": now.isoformat(),
        "acked_at": None,
    }
    save_inbox(inbox_items)
    log_task({"event": "relay_task", "id": item_id, "caller": caller,
              "task": task_text[:200]})
    log.info("Relayed task %s to assistant inbox", item_id)
    return jsonify(ok=True, id=item_id)


@app.route("/inbox", methods=["GET"])
def inbox():
    denied = require_bearer()
    if denied:
        return denied
    pending = [i for i in inbox_items.values() if not i.get("acked_at")]
    pending.sort(key=lambda i: i["received_at"])
    return jsonify(tasks=pending)


@app.route("/inbox/ack", methods=["POST"])
def inbox_ack():
    denied = require_bearer()
    if denied:
        return denied
    data = request.get_json(force=True) or {}
    item_id = (data.get("id") or "").strip()
    item = inbox_items.get(item_id)
    if not item:
        return jsonify(ok=False, error="unknown id"), 404
    item["acked_at"] = datetime.now(timezone.utc).isoformat()
    save_inbox(inbox_items)
    return jsonify(ok=True)


@app.route("/tasks", methods=["GET"])
def tasks():
    denied = require_bearer()
    if denied:
        return denied
    if not os.path.exists(TASK_LOG):
        return jsonify(tasks=[])
    with open(TASK_LOG) as f:
        lines = [json.loads(l) for l in f if l.strip()]
    return jsonify(tasks=lines[-50:])


if __name__ == "__main__":
    log.info("config: ELEVENLABS_API_KEY=%s OUTBOUND_AGENT_ID=%s "
             "PHONE_NUMBER_ID=%s AARON_CALLER_NUMBER=%s INBOX_TOKEN=%s",
             bool(EL_KEY), bool(OUTBOUND_AGENT_ID),
             bool(PHONE_NUMBER_ID), bool(AARON_CALLER_NUMBER),
             bool(INBOX_TOKEN))
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
