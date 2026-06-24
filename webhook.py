"""LINE webhook — the single entry point for your LINE OA.

Does two jobs in one place (LINE allows only ONE webhook URL per channel):
  1. Answers 1:1 customer messages via the AI brain (brain.py + your KB).
  2. Records every inbound event to data/messages.jsonl, which active_list.py
     uses to build an audience of people who engaged with you.

Group messages drive the admin controls: /kb to teach the bot, /done to resume
after you've handled a case yourself. See README.md.

Run:  uvicorn webhook:app --host 0.0.0.0 --port 8650
"""
import base64
import hashlib
import hmac
import json
import logging
import time

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request

import line_api
from brain import run_brain

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("webhook")

DATA_DIR = line_api.DATA_DIR
KB_DIR = line_api.KB_DIR
STATE_FILE = DATA_DIR / "state.json"
MSG_LOG = DATA_DIR / "messages.jsonl"
CONV_DIR = DATA_DIR / "conv"
CONV_DIR.mkdir(exist_ok=True)
LOCK_TIMEOUT = 24 * 3600   # auto-resume a handed-off customer after 24h
HISTORY_TURNS = 12

app = FastAPI()


# ---------- state ----------

def load_state():
    st = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    st.setdefault("admin_group_id", None)
    st.setdefault("locked", {})
    return st


def save_state(st):
    STATE_FILE.write_text(json.dumps(st, ensure_ascii=False, indent=2))


def unlock_expired(st):
    now = int(time.time())
    expired = [u for u, i in st["locked"].items() if now - i["since"] > LOCK_TIMEOUT]
    for u in expired:
        del st["locked"][u]
    if expired:
        save_state(st)
    return st


# ---------- conversation history ----------

def conv_file(uid):
    return CONV_DIR / f"{uid}.jsonl"


def conv_append(uid, role, text):
    with conv_file(uid).open("a") as f:
        f.write(json.dumps({"ts": int(time.time()), "role": role, "text": text},
                           ensure_ascii=False) + "\n")


def conv_history(uid, turns=HISTORY_TURNS):
    f = conv_file(uid)
    if not f.exists():
        return []
    return [json.loads(line) for line in f.read_text().splitlines()[-turns:]]


# ---------- LINE helpers ----------

def send_reply(reply_token, uid, text):
    msgs = [{"type": "text", "text": text}]
    try:
        line_api.line_post("/v2/bot/message/reply",
                           {"replyToken": reply_token, "messages": msgs})
    except Exception:
        line_api.line_post("/v2/bot/message/push", {"to": uid, "messages": msgs})


def push_to_group(text):
    gid = load_state().get("admin_group_id")
    if not gid:
        log.warning("no admin group yet, dropping: %s", text[:60])
        return
    line_api.line_post("/v2/bot/message/push",
                       {"to": gid, "messages": [{"type": "text", "text": text}]})


def display_name(uid):
    try:
        return line_api.line_get(f"/v2/bot/profile/{uid}").get("displayName", uid[:8])
    except Exception:
        return uid[:8]


def lock_user(uid, name, reason):
    st = load_state()
    st["locked"][uid] = {"name": name, "reason": reason, "since": int(time.time())}
    save_state(st)


# ---------- event handlers ----------

def handle_user_message(event):
    uid = event["source"]["userId"]
    reply_token = event.get("replyToken", "")
    msg = event.get("message", {})
    st = unlock_expired(load_state())

    # Waiting for a human → relay to the admin group, never answer.
    if uid in st["locked"]:
        name = st["locked"][uid]["name"]
        shown = msg.get("text") if msg.get("type") == "text" else f"[{msg.get('type')}]"
        push_to_group(f"💬 {name} (waiting for you): \"{shown}\"")
        return

    name = display_name(uid)

    # Only text goes to the brain. Anything else (image/file — could be a payment
    # slip) is escalated to a human to be safe.
    if msg.get("type") != "text":
        if msg.get("type") == "sticker":
            return
        lock_user(uid, name, f"sent a {msg.get('type')} (could be a receipt/doc)")
        push_to_group(f"🔔 {name} sent a {msg.get('type')} — please take a look. "
                      f"Type /done when handled.")
        send_reply(reply_token, uid,
                   "Got it — someone from our team will take a look shortly. 🙏")
        return

    text = msg["text"]
    conv_append(uid, "user", text)
    result = run_brain(name, conv_history(uid)[:-1], text)
    reply = result.get("reply", "")
    send_reply(reply_token, uid, reply)
    conv_append(uid, "assistant", reply)

    if result.get("handoff"):
        lock_user(uid, name, result.get("handoff_reason", ""))
        push_to_group(
            f"🔔 Handoff\nCustomer: {name}\nReason: {result.get('handoff_reason', '')}\n"
            f"Last message: \"{text}\"\n\nThe bot has paused for this person. "
            f"Type /done here once you've replied."
        )


def handle_group_message(event):
    gid = event["source"]["groupId"]
    text = event.get("message", {}).get("text", "")
    st = load_state()
    if st.get("admin_group_id") != gid:
        st["admin_group_id"] = gid
        save_state(st)
        push_to_group("✅ Connected to this group — this is now your control room.")

    tokens = text.split()

    # /kb <text> — append the rest of the message to the KB.
    if "/kb" in tokens:
        content = " ".join(t for t in tokens if t != "/kb").strip()
        if not content:
            push_to_group("Add the knowledge after /kb, e.g. /kb We ship worldwide.")
            return
        learned = KB_DIR / "90-learned.md"
        if not learned.exists():
            learned.write_text("# Things the admin taught the bot (via /kb)\n")
        with learned.open("a") as f:
            f.write(f"\n- ({time.strftime('%Y-%m-%d')}) {content}\n")
        push_to_group(f"✅ Saved to the KB:\n\"{content}\"")
        return

    # /done — clear all open cases; the bot resumes answering everyone.
    if "/done" in tokens:
        st = load_state()
        if not st["locked"]:
            push_to_group("✅ No open cases right now.")
            return
        names = [i["name"] for i in st["locked"].values()]
        st["locked"] = {}
        save_state(st)
        push_to_group(f"✅ Cleared ({', '.join(names)}). The bot is answering again.")


# ---------- webhook ----------

@app.get("/health")
def health():
    st = load_state()
    return {"ok": True, "admin_group": bool(st.get("admin_group_id")),
            "locked": len(st["locked"])}


@app.post("/webhook")
async def webhook(request: Request, background: BackgroundTasks,
                  x_line_signature: str = Header(default="")):
    body = await request.body()
    expected = base64.b64encode(
        hmac.new(line_api.CHANNEL_SECRET.encode(), body, hashlib.sha256).digest()
    ).decode()
    if not hmac.compare_digest(expected, x_line_signature):
        raise HTTPException(status_code=403, detail="bad signature")

    for event in json.loads(body).get("events", []):
        src = event.get("source", {})
        # Log every event for the active-list builder (Group A = messagers/followers).
        with MSG_LOG.open("a") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
        if src.get("type") == "group" and event.get("type") in ("message", "join"):
            background.add_task(handle_group_message, event)
        elif src.get("type") == "user" and event.get("type") == "message":
            background.add_task(handle_user_message, event)
    return {"ok": True}
