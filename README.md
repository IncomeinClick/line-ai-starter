# line-ai-starter

A small, self-contained starter for running a **LINE Official Account** with:

1. **An AI chat brain** that answers your customers' 1:1 messages from a knowledge
   base you control (the `kb/` folder), and hands off to you when a human is needed.
2. **An active-list / broadcast system** that builds an audience of the people who
   actually engage with you, so you can broadcast and narrowcast without wasting
   your monthly message quota on dead followers.

Both run from a **single webhook** (LINE allows only one webhook URL per channel).

Everything is plain Python (standard library + FastAPI). No database, no framework —
just files under `data/`.

---

## How it fits together

```
            ┌─────────────────────────────────────────────┐
  LINE  ──▶ │ webhook.py   (one URL, two jobs)             │
            │   • answers customers via brain.py + kb/     │
            │   • logs every event → data/messages.jsonl   │
            └───────────────┬─────────────────────────────┘
                            │
   broadcast.py ───────────▶│ data/broadcast.log  (requestIds)
                            ▼
   active_list.py ──▶ builds LINE audiences (who messaged / who opened)
                            ▼
   narrowcast.py ───▶ sends only to that active audience
```

---

## Prerequisites

- Python 3.10+
- A **LINE Official Account** with the Messaging API enabled (you'll get a *channel
  access token* and a *channel secret*)
- The **Claude CLI** installed and logged in (the chat brain shells out to
  `claude -p`). If you'd rather use a different LLM, replace `run_brain()` in
  `brain.py` — that's the only place the model is called.
- A public HTTPS URL for the webhook (a server, or a tunnel like ngrok/Cloudflare
  Tunnel for testing)

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env
# then edit .env and paste your LINE channel access token + secret
```

## 1. Run the webhook

```bash
uvicorn webhook:app --host 0.0.0.0 --port 8650
```

In the LINE Developers console, set your channel's **webhook URL** to
`https://YOUR_DOMAIN/webhook` and turn "Use webhook" on. Check it's alive at
`https://YOUR_DOMAIN/health`.

> **Also in the LINE Official Account Manager** → **Settings → Response settings**:
> turn **Webhooks ON**, allow the bot to reply, and turn **Auto-reply messages** and
> **Greeting messages OFF** — otherwise LINE's built-in canned replies fire alongside
> your bot.

### Your control room (works even if you're a solo operator)

The bot needs one place to notify you and take commands. Create a **LINE group**,
add your Official Account to it, and send any message — the bot will reply
"Connected to this group". That group is now your control room. You don't need a
team; a group of just **you + the bot** works exactly the same.

When the bot hands a conversation off to you:

1. You get a notification in the control group.
2. Open the **LINE Official Account Manager** (app or web) and reply to that
   customer directly.
3. Type `/done` in the control group → the bot resumes answering.

### Commands (type these in the control group)

- `/kb <text>` — teach the bot a new fact (appended to `kb/90-learned.md`)
- `/done` — clear all open hand-offs; the bot answers everyone again

### The knowledge base

Edit the markdown files in `kb/` — that's everything the bot knows. Start with
`kb/00-about.md`, `kb/10-faq.md`, and `kb/20-handoff-rules.md`. The bot reads all of
them on every message, so keep them tight and plain.

## 2. Broadcasting

```bash
# write your message in a text file, then dry-run it:
python broadcast.py message.txt
# send for real:
python broadcast.py message.txt --now
```

A broadcast goes to **all** your followers and costs roughly that many messages
against your monthly quota. Each send is logged so the active-list builder can later
turn the openers into a reusable audience.

> **Tracking tip:** LINE does **not** track link clicks the way email does. The only
> way to know traffic/sales came from LINE is to put a UTM tag on every link, e.g.
> `https://your-site.com/?utm_source=line&utm_medium=broadcast&utm_campaign=launch`

## 3. Building the active list

`active_list.py` maintains two audiences:

- **Group A** — everyone who messaged or followed you (from `data/messages.jsonl`,
  written by the webhook)
- **Group B** — everyone who opened a past broadcast (built from each broadcast's
  requestId)

```bash
python active_list.py init       # one-time: seed Group A from past messagers
python active_list.py cron       # daily: grow Group A + refresh Group B audiences
python active_list.py stats      # live counts from LINE (real numbers)
```

Run `cron` daily (e.g. via crontab):

```
0 3 * * * cd /path/to/line-ai-starter && python active_list.py cron >> data/cron.log 2>&1
```

## 4. Narrowcasting to the active list

```bash
python narrowcast.py message.txt          # dry run (shows the resolved audience)
python narrowcast.py message.txt --now     # send only to active people
```

A narrowcast reaches **only** people in your active list (Group A OR Group B),
so it costs far less quota than a full broadcast.

### Other ways to narrowcast (no setup required)

You don't strictly need this audience system to narrowcast. LINE also lets you:

- **Target by demographics** (gender, age, region, OS, follow duration) — built into
  LINE, works as soon as you have enough followers, no code needed.
- **Build audiences by hand** in the LINE Official Account Manager (click-based,
  impression-based, chat-tag, or uploaded lists).

This project just automates the impression/upload audiences so you don't have to
rebuild them by hand after every send.

---

## Files

| File | What it does |
|---|---|
| `line_api.py` | Tiny LINE API helper; reads credentials from `.env` |
| `webhook.py` | The single webhook — answers customers + logs events + admin commands |
| `brain.py` | The AI chat brain (KB → reply + handoff decision) |
| `broadcast.py` | Send a broadcast to all followers |
| `active_list.py` | Build & refresh the "active" audiences from LINE |
| `narrowcast.py` | Send only to the active audience |
| `kb/` | Your knowledge base (edit these) |
| `data/` | Local state, logs, conversations (gitignored) |

## Notes

- Everything in `data/` and your `.env` stays local and is gitignored — don't commit
  them.
- If you already run another LINE webhook (e.g. a different chatbot), remember LINE
  allows only one webhook URL per channel. Point it here, or have your existing
  webhook also append inbound events to `data/messages.jsonl` so the active list
  keeps growing.

## License

MIT
