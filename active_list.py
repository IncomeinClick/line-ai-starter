#!/usr/bin/env python3
"""LINE active-list builder — pure code, no AI.

Maintains two kinds of LINE Audiences, meant to run from a daily cron:

  Group A (Upload)     : people who MESSAGED or newly FOLLOWED you. We own their
                         userIds (from data/messages.jsonl, written by webhook.py),
                         so we grow one upload audience over time via PUT.
  Group B (Impression) : people who OPENED a past broadcast. LINE only exposes
                         these as an opaque "impression audience" built from a
                         broadcast's requestId. Impression audiences can't be
                         appended to, so each cron run RE-CREATES a fresh one
                         (cumulative) per broadcast and deletes the previous copy,
                         until the requestId ages out (~60 days) and we freeze it.

To actually reach "active" people, narrowcast to (A OR B1 OR B2 ...) — see
narrowcast.py.

Commands:
  init      create Group A from everyone who messaged/followed since START_DATE
  cron      daily: grow Group A from new events + refresh all Group B audiences
  snapshot  query LINE for live counts, write data/stats.json
  stats     print current state + live counts as JSON
"""
import json
import os
import sys
import time
import urllib.error
from datetime import datetime, timezone, timedelta

from line_api import line_api as api, DATA_DIR

TZ = timezone(timedelta(hours=int(os.environ.get("TZ_OFFSET", "7"))))   # default Asia/Bangkok
START_DATE = datetime.fromisoformat(
    os.environ.get("ACTIVE_LIST_START", "2026-01-01")).replace(tzinfo=TZ)
IMP_WINDOW_DAYS = 60                                # requestId usable for ~60 days
MSG_LOG = DATA_DIR / "messages.jsonl"
BROADCAST_LOG = DATA_DIR / "broadcast.log"
STATE_FILE = DATA_DIR / "active_list.json"
STATS_FILE = DATA_DIR / "stats.json"

QUALIFYING_TYPES = ("message", "follow")           # what counts as "active" inbound


# ---------- state ----------

def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"group_a_id": None, "watermark": 0, "broadcasts": {}, "last_run": None}


def save_state(st):
    st["last_run"] = datetime.now(TZ).isoformat(timespec="seconds")
    STATE_FILE.write_text(json.dumps(st, ensure_ascii=False, indent=2))


# ---------- inbound events ----------

def iter_events(since_ms=0):
    """Yield (ts_ms, userId) for qualifying inbound events after since_ms.

    Only real 1:1 user events count — group/admin events are skipped.
    """
    if not MSG_LOG.exists():
        return
    for line in MSG_LOG.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("type") not in QUALIFYING_TYPES:
            continue
        src = ev.get("source") or {}
        if src.get("type") != "user":
            continue
        uid = src.get("userId")
        ts = ev.get("timestamp", 0)
        if uid and ts > since_ms:
            yield ts, uid


# ---------- Group A (upload audience) ----------

def chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def create_group_a(user_ids):
    res = api("POST", "/v2/bot/audienceGroup/upload", {
        "description": "active_list",
        "isIfaAudience": False,
        "audiences": [{"id": u} for u in user_ids[:1000]],
    })
    gid = res["audienceGroupId"]
    for batch in chunks(user_ids[1000:], 1000):
        api("PUT", "/v2/bot/audienceGroup/upload",
            {"audienceGroupId": gid, "audiences": [{"id": u} for u in batch]})
    return gid


def add_to_group_a(gid, user_ids):
    for batch in chunks(user_ids, 1000):
        api("PUT", "/v2/bot/audienceGroup/upload",
            {"audienceGroupId": gid, "audiences": [{"id": u} for u in batch]})


def update_group_a(st):
    """Grow Group A from events newer than the watermark. Returns (#new, new_watermark)."""
    new_ids, max_ts = [], st["watermark"]
    seen = set()
    for ts, uid in iter_events(st["watermark"]):
        max_ts = max(max_ts, ts)
        if uid not in seen:
            seen.add(uid)
            new_ids.append(uid)
    if not new_ids:
        return 0, st["watermark"]
    if st.get("group_a_id"):
        add_to_group_a(st["group_a_id"], new_ids)
    else:
        st["group_a_id"] = create_group_a(new_ids)
    st["watermark"] = max_ts
    return len(new_ids), max_ts


# ---------- Group B (impression audiences) ----------

def parse_broadcasts():
    """Read broadcast.log → list of {request_id, sent_ts_ms, date}."""
    out = []
    if not BROADCAST_LOG.exists():
        return out
    for line in BROADCAST_LOG.read_text().splitlines():
        if "requestId=" not in line:
            continue
        parts = [p.strip() for p in line.split("|")]
        try:
            sent = datetime.strptime(parts[0], "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
        except (ValueError, IndexError):
            continue
        rid = line.split("requestId=", 1)[1].strip()
        if not rid or rid == "None":
            continue
        out.append({"request_id": rid, "sent_ts_ms": int(sent.timestamp() * 1000),
                    "date": sent.strftime("%Y%m%d")})
    return out


def refresh_group_b(st):
    """Re-create one fresh impression audience per in-window broadcast; freeze old ones.

    Returns a short report list for logging.
    """
    now = time.time()
    report = []
    for bc in parse_broadcasts():
        rid = bc["request_id"]
        rec = st["broadcasts"].setdefault(rid, {
            "sent_ts": bc["sent_ts_ms"], "date": bc["date"],
            "imp_group_id": None, "frozen": False,
        })
        if rec.get("frozen"):
            continue
        age_days = (now - bc["sent_ts_ms"] / 1000) / 86400
        if age_days > IMP_WINDOW_DAYS:
            rec["frozen"] = True            # keep last good snapshot, stop refreshing
            report.append(f"{bc['date']}: frozen (>{IMP_WINDOW_DAYS}d)")
            continue
        try:
            res = api("POST", "/v2/bot/audienceGroup/imp", {
                "description": f"imp_{bc['date']}_{rid[:6]}",
                "requestId": rid,
            })
            new_gid = res["audienceGroupId"]
        except urllib.error.HTTPError as e:
            report.append(f"{bc['date']}: imp create failed {e.code} {e.read().decode()[:120]}")
            continue
        old_gid = rec.get("imp_group_id")
        rec["imp_group_id"] = new_gid
        if old_gid and old_gid != new_gid:
            try:
                api("DELETE", f"/v2/bot/audienceGroup/{old_gid}")
            except urllib.error.HTTPError:
                pass
        report.append(f"{bc['date']}: imp refreshed -> {new_gid}")
    return report


# ---------- live counts ----------

def narrowcast_reach():
    """Real DEDUPED reach per past narrowcast, from LINE's progress endpoint.

    Impression audiences are opaque (we can't list/intersect their members), so the
    only true deduped number LINE gives us is successCount from the narrowcast
    progress endpoint — and only AFTER a send. Returns newest-first.
    """
    out = []
    if not BROADCAST_LOG.exists():
        return out
    for line in BROADCAST_LOG.read_text().splitlines():
        if "narrowcast" not in line or "requestId=" not in line:
            continue
        parts = [p.strip() for p in line.split("|")]
        try:
            date = datetime.strptime(parts[0], "%Y-%m-%d %H:%M").strftime("%Y%m%d")
        except (ValueError, IndexError):
            date = ""
        rid = line.split("requestId=", 1)[1].strip()
        if not rid or rid == "None":
            continue
        try:
            p = api("GET", f"/v2/bot/message/progress/narrowcast?requestId={rid}")
        except urllib.error.HTTPError:
            continue  # requestId aged out / not a narrowcast — skip
        if p.get("phase") == "succeeded":
            out.append({"date": date, "request_id": rid, "reach": p.get("successCount", 0)})
    out.sort(key=lambda x: x["date"], reverse=True)
    return out


def audience_index():
    """Map audienceGroupId -> {count, status, description, type} from LINE."""
    idx, page = {}, 1
    while True:
        res = api("GET", f"/v2/bot/audienceGroup/list?page={page}&size=40")
        for g in res.get("audienceGroups", []):
            idx[g["audienceGroupId"]] = {
                "count": g.get("audienceCount", 0), "status": g.get("status"),
                "description": g.get("description"), "type": g.get("type"),
            }
        if not res.get("hasNextPage"):
            break
        page += 1
    return idx


def build_snapshot(st):
    idx = audience_index()
    a_id = st.get("group_a_id")
    a = idx.get(a_id, {})
    broadcasts = []
    for rid, rec in sorted(st["broadcasts"].items(), key=lambda kv: kv[1]["date"], reverse=True):
        gid = rec.get("imp_group_id")
        info = idx.get(gid, {})
        broadcasts.append({
            "date": rec["date"], "frozen": rec.get("frozen", False),
            "imp_group_id": gid, "opens": info.get("count", 0),
            "status": info.get("status"),
        })
    try:
        quota = api("GET", "/v2/bot/message/quota/consumption").get("totalUsage", 0)
    except urllib.error.HTTPError:
        quota = None
    followers = {}
    for d in range(1, 5):  # insight lags a day or two; try recent dates
        day = (datetime.now(TZ) - timedelta(days=d)).strftime("%Y%m%d")
        try:
            f = api("GET", f"/v2/bot/insight/followers?date={day}")
            if f.get("status") == "ready":
                followers = {"followers": f.get("followers"), "reachable": f.get("targetedReaches"),
                             "blocked": f.get("blocks"), "date": day}
                break
        except urllib.error.HTTPError:
            continue
    group_a = {"id": a_id, "count": a.get("count", 0), "status": a.get("status")}
    approx_active = group_a["count"] + sum(b["opens"] for b in broadcasts)
    reaches = narrowcast_reach()
    snap = {
        "updated": datetime.now(TZ).isoformat(timespec="seconds"),
        "quota_used": quota,
        "followers": followers,
        "group_a": group_a,
        "broadcasts": broadcasts,
        "approx_active_sum": approx_active,
        "narrowcast_reach": reaches,
        "last_real_reach": reaches[0]["reach"] if reaches else None,
        "last_run": st.get("last_run"),
    }
    STATS_FILE.write_text(json.dumps(snap, ensure_ascii=False, indent=2))
    return snap


# ---------- commands ----------

def cmd_init():
    st = load_state()
    if st.get("group_a_id"):
        print(f"Group A already exists: {st['group_a_id']} — use 'cron' to grow it.")
        return
    ids, max_ts, seen = [], 0, set()
    start_ms = int(START_DATE.timestamp() * 1000)
    for ts, uid in iter_events(start_ms - 1):
        max_ts = max(max_ts, ts)
        if uid not in seen:
            seen.add(uid)
            ids.append(uid)
    if not ids:
        print("No qualifying users since START_DATE — nothing to create yet.")
        return
    st["group_a_id"] = create_group_a(ids)
    st["watermark"] = max_ts
    save_state(st)
    print(f"Created Group A {st['group_a_id']} with {len(ids)} users (watermark={max_ts}).")


def cmd_cron():
    st = load_state()
    n_new, _ = update_group_a(st)
    b_report = refresh_group_b(st)
    save_state(st)
    build_snapshot(st)
    print(f"[{datetime.now(TZ):%Y-%m-%d %H:%M}] Group A +{n_new} new. Group B: {b_report or 'none'}")


def cmd_snapshot():
    st = load_state()
    print(json.dumps(build_snapshot(st), ensure_ascii=False, indent=2))


def cmd_stats():
    st = load_state()
    print(json.dumps({"state": st, "snapshot": build_snapshot(st)}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    {"init": cmd_init, "cron": cmd_cron, "snapshot": cmd_snapshot, "stats": cmd_stats}.get(
        cmd, lambda: print(__doc__))()
