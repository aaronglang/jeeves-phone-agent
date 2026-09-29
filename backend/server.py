"""Phone-agent backend: receives the inbound agent's webhook tool calls and
fires ElevenLabs outbound calls. Also relays non-call tasks from the
inbound agent to Aaron's personal assistant via a polled inbox.

Endpoints:
  POST /tools/request-call   <- ElevenLabs webhook tool (inbound agent).
                               caller_number is REQUIRED and must be Aaron
                               (principal) or Rosalie (near-principal).
  POST /tools/relay-task     <- ElevenLabs webhook tool: hand a non-call
                               task to the assistant. Open to ANY caller;
                               caller type is recorded, not enforced.
                               Optional conversation_id is stored and
                               prefixed onto the task as [conv:<id>].
  GET  /health               <- public liveness check (keep-warm pings this)
  GET  /tasks                <- recent task log (bearer token required)
  GET  /inbox                <- unacknowledged relayed tasks (bearer token)
  POST /inbox/ack            <- acknowledge a relayed task (bearer token)

Env: ELEVENLABS_API_KEY, OUTBOUND_AGENT_ID, PHONE_NUMBER_ID,
     AARON_CALLER_NUMBER (default +17016091267),
     ROSALIE_CALLER_NUMBER (default +14805935327),
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
AARON_CALLER_NUMBER = os.environ.get("AARON_CALLER_NUMBER") or "+17016091267"
ROSALIE_CALLER_NUMBER = os.environ.get("ROSALIE_CALLER_NUMBER") or "+14805935327"
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


def caller_type(number):
    """Classify a phone number as aaron / rosalie / third_party."""
    if number and number == AARON_CALLER_NUMBER:
        return "aaron"
    if number and number == ROSALIE_CALLER_NUMBER:
        return "rosalie"
    return "third_party"


def check_caller(caller):
    """Fail-closed authorization for principals (Aaron, Rosalie).
    Returns an error response or None."""
    if not caller:
        return jsonify(ok=False, error="caller_number is required"), 400
    if caller_type(caller) == "third_party":
        log.warning("Rejected: caller not authorized")
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


REQUIRED_CALL_VARS = ("callback_topic", "callback_summary",
                      "callee_type", "callee_name")


def call_variables(to_number, task_brief, contact_name):
    """Build the dynamic variables every outbound call must carry."""
    callee = caller_type(to_number)
    default_name = {"aaron": "Aaron", "rosalie": "Rosalie"}.get(callee, "there")
    first_line = task_brief.strip().split(".")[0][:160]
    return {
        "task_brief": task_brief,
        "callback_topic": first_line or contact_name or "a matter for Aaron",
        "callback_summary": task_brief or first_line or "No summary provided.",
        "callee_type": callee,
        "callee_name": contact_name or default_name,
    }


def opener(variables):
    """Jeeves identifies as Aaron's butler on every call."""
    if variables["callee_type"] == "aaron":
        return f"Jeeves here, Aaron. {variables['callback_topic']}."
    if variables["callee_type"] == "rosalie":
        return f"Jeeves here, Rosalie. {variables['callback_topic']}."
    return (f"I'm Jeeves — Aaron's butler. "
            f"I'm calling regarding {variables['callback_topic']}.")


def elevenlabs_outbound(to_number, task_brief, contact_name):
    """Trigger an ElevenLabs outbound call. Returns (ok, payload)."""
    variables = call_variables(to_number, task_brief, contact_name)
    missing = [k for k in REQUIRED_CALL_VARS
               if not isinstance(variables.get(k), str) or not variables[k].strip()]
    if missing:
        # A missing dynamic variable crashes the call; never place one.
        log.error("outbound call refused: missing dynamic variables %s", missing)
        return False, {"error": "missing dynamic variables", "missing": missing}
    body = {
        "agent_id": OUTBOUND_AGENT_ID,
        "agent_phone_number_id": PHONE_NUMBER_ID,
        "to_number": to_number,
        "conversation_initiation_client_data": {
            "dynamic_variables": variables
        },
        "conversation_config_override": {
            "agent": {
                "first_message": opener(variables)
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
                      "caller": caller, "caller_type": caller_type(caller)})
        return denied
    if not to_number or not task_brief:
        return jsonify(ok=False, error="to_number and task_brief are required"), 400
    if not EL_KEY or not OUTBOUND_AGENT_ID or not OUTBOUND_AGENT_ID or not PHONE_NUMBER_ID:
        log.error("request-call refused: ElevenLabs config missing")
        return jsonify(ok=False, error="server missing ElevenLabs config"), 500

    ok, payload = elevenlabs_outbound(to_number, task_brief, contact_name)
    log_task({
        "event": "outbound_call",
        "ok": ok,
        "to_number": to_number,
        "contact_name": contact_name,
        "task_brief": task_brief,
        "caller": caller,
        "caller_type": caller_type(caller),
        "callee_type": caller_type(to_number),
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
    conversation_id = str(data.get("conversation_id") or "").strip() or None
    ctype = caller_type(caller)

    # Relay is open to any caller; caller type is recorded, not enforced.
    if not task_text:
        return jsonify(ok=False, error="task is required"), 400
    if conversation_id and conversation_id not in task_text:
        task_text = f"[conv:{conversation_id}] {task_text}"

    now = datetime.now(timezone.utc)
    item_id = "inbox-" + now.strftime("%Y%m%d%H%M%S") + "-" + os.urandom(3).hex()
    inbox_items[item_id] = {
        "id": item_id,
        "task": task_text,
        "callback_note": callback_note,
        "caller": caller,
        "caller_type": ctype,
        "conversation_id": conversation_id,
        "received_at": now.isoformat(),
        "acked_at": None,
    }
    save_inbox(inbox_items)
    log_task({"event": "relay_task", "id": item_id, "caller": caller,
              "caller_type": ctype, "conversation_id": conversation_id,
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
             "PHONE_NUMBER_ID=%s AARON_CALLER_NUMBER=%s "
             "ROSALIE_CALLER_NUMBER=%s INBOX_TOKEN=%s",
             bool(EL_KEY), bool(OUTBOUND_AGENT_ID),
             bool(PHONE_NUMBER_ID), bool(AARON_CALLER_NUMBER),
             bool(ROSALIE_CALLER_NUMBER),
             bool(INBOX_TOKEN))
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
