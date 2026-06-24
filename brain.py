"""The AI chat brain.

Turns an incoming customer message + your KB (the kb/ folder) into a reply, and
decides whether to hand the conversation off to a human. Runs the Claude CLI
(`claude -p`) under the hood, so you need the Claude CLI installed and logged in.
Swap run_brain() if you'd rather call a different LLM or API.

Output is a JSON object: {reply, handoff, handoff_reason}.
"""
import json
import logging
import os
import re
import subprocess

import line_api

log = logging.getLogger("brain")

CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")
BRAIN_MODEL = line_api.ENV.get("BRAIN_MODEL", "sonnet")
BRAIN_TIMEOUT = 180


def load_kb():
    if not line_api.KB_DIR.exists():
        return ""
    return "\n\n".join(
        f"<!-- {p.name} -->\n{p.read_text()}"
        for p in sorted(line_api.KB_DIR.glob("*.md"))
    )


def build_prompt(user_name, history, text):
    hist = "\n".join(
        f"{'Customer' if h['role'] == 'user' else 'Assistant'}: {h['text']}"
        for h in history
    )
    return f"""You are a friendly customer-support assistant for a small business,
answering 1:1 chat messages on LINE.

Everything you know about the business is in this knowledge base (KB):
{load_kb() or '(KB is empty — fill in the kb/ folder)'}

Recent chat history with this customer (name: {user_name}):
{hist or '(no prior messages)'}

New message from the customer:
{text}

Reply as a JSON object with exactly these keys:
- reply (string): your answer to the customer. Plain text, friendly, concise, like
  a real person in a chat. Reply in the SAME language the customer wrote in. No
  markdown, no ** bold **, no [text](url) — paste raw URLs if needed.
- handoff (boolean): true ONLY when you must escalate to a human — i.e. (a) anything
  about money / payment / receipts / refunds / billing / account status, or (b) a
  question about specific internal info you genuinely cannot know from the KB (real
  prices, this customer's order status, internal details not in the KB). General
  knowledge or terminology you simply don't recognise is NOT a reason to hand off —
  answer it yourself or ask a clarifying question.
- handoff_reason (string): if handoff is true, a one-line summary for the human;
  otherwise "".

When handoff is true, make `reply` a polite note that you've passed it on and a
human will follow up shortly.

Return ONLY the JSON object — no other text, no ``` fences."""


def _strip_fence(txt):
    txt = txt.strip()
    if txt.startswith("```"):
        txt = txt.strip("`")
        if txt[:4].lower() == "json":
            txt = txt[4:]
        txt = txt.strip()
    return txt


def _parse(stdout):
    """Pull the brain's JSON object out of a `claude -p --output-format json` envelope."""
    env = json.loads(stdout)
    txt = env.get("result", "") if isinstance(env, dict) else stdout
    txt = _strip_fence(txt)
    try:
        return json.loads(txt)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", txt, re.S)
        if not m:
            raise
        return json.loads(m.group(0))


def _default_handoff(reason):
    return {"reply": "Thanks! I've passed this to our team — someone will follow up "
                     "with you shortly.",
            "handoff": True, "handoff_reason": reason}


def run_brain(user_name, history, text):
    prompt = build_prompt(user_name, history, text)
    cmd = [CLAUDE_BIN, "-p", prompt, "--model", BRAIN_MODEL,
           "--output-format", "json", "--max-turns", "1", "--allowedTools", ""]
    for attempt in (1, 2):
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=BRAIN_TIMEOUT)
            if proc.returncode != 0:
                log.warning("claude exit %s: %s", proc.returncode,
                            (proc.stderr or proc.stdout)[-300:])
                continue
            return _parse(proc.stdout)
        except Exception:
            log.warning("brain attempt %d/2 failed", attempt)
    # Couldn't get a clean answer — escalate to a human rather than stay silent.
    return _default_handoff("the assistant could not answer automatically")
