"""Minimal LINE Messaging API helper.

Reads your channel credentials from a local .env file (see .env.example) and
exposes small wrappers around the LINE API used across this project. No secrets
are hard-coded — everything comes from .env.
"""
import json
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent
KB_DIR = BASE / "kb"
DATA_DIR = BASE / "data"
DATA_DIR.mkdir(exist_ok=True)


def load_env():
    env = {}
    env_file = BASE / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env


ENV = load_env()
ACCESS_TOKEN = ENV.get("LINE_CHANNEL_ACCESS_TOKEN", "")
CHANNEL_SECRET = ENV.get("LINE_CHANNEL_SECRET", "")


def line_get(path):
    req = urllib.request.Request(
        f"https://api.line.me{path}",
        headers={"Authorization": f"Bearer {ACCESS_TOKEN}"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = resp.read()
        return json.loads(body) if body else {}


def line_post(path, payload):
    req = urllib.request.Request(
        f"https://api.line.me{path}",
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {ACCESS_TOKEN}",
                 "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.headers.get("X-Line-Request-Id")


def line_api(method, path, payload=None):
    """Generic call; returns parsed JSON body (or {} for an empty 200)."""
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Authorization": f"Bearer {ACCESS_TOKEN}"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(f"https://api.line.me{path}", data=data,
                                 headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = resp.read()
        return json.loads(body) if body else {}
