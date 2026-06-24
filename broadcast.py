"""Send a LINE broadcast (to ALL followers) from a text file.

Usage: python broadcast.py <message.txt> [--now]
Without --now it just prints the message (dry run).
"""
import sys
from datetime import datetime

import line_api

BROADCAST_LOG = line_api.DATA_DIR / "broadcast.log"
LATEST_KB = line_api.KB_DIR / "50-latest-broadcast.md"


def record(path, request_id, kind="broadcast"):
    """Log the requestId (so active_list.py can build an openers audience) and keep
    the latest message in the KB so the chat brain understands replies to it."""
    with open(BROADCAST_LOG, "a") as f:
        f.write(f"{datetime.now():%Y-%m-%d %H:%M} | {path.split('/')[-1]} | "
                f"{kind} | requestId={request_id}\n")
    line_api.KB_DIR.mkdir(exist_ok=True)
    text = open(path).read().strip()
    LATEST_KB.write_text(
        f"# Latest broadcast (sent to followers)\n\n"
        f"Sent: {datetime.now():%Y-%m-%d %H:%M}\n"
        f"If a customer replies referring to this, they just received it:\n\n---\n\n{text}\n"
    )


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    path = sys.argv[1]
    text = open(path).read().strip()
    if "--now" not in sys.argv:
        print("--- DRY RUN (pass --now to send) ---")
        print(text)
        sys.exit(0)
    request_id = line_api.line_post(
        "/v2/bot/message/broadcast", {"messages": [{"type": "text", "text": text}]})
    record(path, request_id)
    used = line_api.line_get("/v2/bot/message/quota/consumption")["totalUsage"]
    print(f"Broadcast sent. requestId={request_id}")
    print(f"Quota used this month: {used:,}")
