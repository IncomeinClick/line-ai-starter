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


# LINE caps a narrowcast recipient at 10 audiences in an `or` operator — go over and the
# whole send fails with 400 "audience can not be used more than 10 times".
LINE_AUDIENCE_CAP = 10


def active_audience_ids():
    """Group A id + the newest non-frozen impression-audience ids, capped at LINE's limit.

    The active list grows by one impression audience per broadcast, so it eventually exceeds
    LINE's 10-audience cap. Impression audiences overlap heavily (repeat openers), so we keep
    Group A plus the most-recent impression audiences (newest = most engaged) up to the cap.
    """
    if not STATE_FILE.exists():
        return []
    st = json.loads(STATE_FILE.read_text())
    ids = []
    if st.get("group_a_id"):
        ids.append(int(st["group_a_id"]))
    imps = [b for b in st.get("broadcasts", {}).values()
            if not b.get("frozen") and b.get("imp_group_id")]
    imps.sort(key=lambda b: b.get("sent_ts", 0), reverse=True)
    for b in imps[:LINE_AUDIENCE_CAP - len(ids)]:
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
