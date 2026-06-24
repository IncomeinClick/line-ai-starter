"""Send a LINE narrowcast to your ACTIVE list from a text file.

The active list = people who messaged/followed you (Group A) OR opened a past
broadcast (Group B), read live from data/active_list.json (built by active_list.py).

Usage: python narrowcast.py <message.txt> [--now]
Without --now it prints the message + the resolved audience (dry run).
"""
import json
import sys

import line_api
from broadcast import record

STATE_FILE = line_api.DATA_DIR / "active_list.json"


def active_audience_ids():
    """Group A id + every non-frozen impression-audience id, from active_list state."""
    if not STATE_FILE.exists():
        return []
    st = json.loads(STATE_FILE.read_text())
    ids = []
    if st.get("group_a_id"):
        ids.append(int(st["group_a_id"]))
    for b in st.get("broadcasts", {}).values():
        if not b.get("frozen") and b.get("imp_group_id"):
            ids.append(int(b["imp_group_id"]))
    return ids


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    path = sys.argv[1]
    text = open(path).read().strip()
    ids = active_audience_ids()
    if not ids:
        print("No active audience yet — run active_list.py (init / cron) first.")
        sys.exit(1)
    recipient = {"type": "operator",
                 "or": [{"type": "audience", "audienceGroupId": g} for g in ids]}
    if "--now" not in sys.argv:
        print("--- DRY RUN (pass --now to send) ---")
        print(f"audience groupIds (A OR B...) = {ids}")
        print("---")
        print(text)
        sys.exit(0)
    request_id = line_api.line_post("/v2/bot/message/narrowcast", {
        "messages": [{"type": "text", "text": text}], "recipient": recipient})
    record(path, request_id, kind=f"narrowcast A+B={ids}")
    used = line_api.line_get("/v2/bot/message/quota/consumption")["totalUsage"]
    print(f"Narrowcast sent. requestId={request_id}")
    print(f"Quota used this month: {used:,}")
