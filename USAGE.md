# RestrictedContentDL — How to Run

This guide covers setup, daily use, and safe shutdown/cleanup.

---

## 1) Prerequisites

- Docker Desktop (or Docker Engine + Compose plugin)
- A Telegram **bot token**
- A Telegram **API ID** and **API HASH**
- A **SESSION_STRING** for your user account

---

## 2) First‑time setup

1) **Clone the repo**
```
git clone https://github.com/bisnuray/RestrictedContentDL
cd RestrictedContentDL
```


### 2.2 Create or edit `config.env`

Open `config.env` and replace the placeholders:

- `API_ID`
- `API_HASH`
- `BOT_TOKEN`
- `SESSION_STRING`

Speed settings:

```env
DOWNLOAD_WORKERS=1
MAX_CONCURRENT_TRANSMISSIONS=4
```

`DOWNLOAD_WORKERS` is the one that matters — but **measure before you raise it.**

Telegram throttles download speed per connection rather than per account, so
splitting a file across several connections to the same datacenter usually
multiplies throughput, with no Premium subscription needed. That is the theory,
and it holds on a stable link.

It does not hold on an unstable one. Parallel connections only pay off if the
network keeps them alive; if connections are being reset, every reset costs a
retry and more workers make the download **slower**. On the connection this was
developed against, 4 workers measured 0.44 MB/s against 1.31 MB/s on a single
connection — three times worse.

So run the benchmark and let it decide:

```sh
python benchmark_speed.py https://t.me/c/1234567890/42
```

It samples a few MB at 1, 2, 4 and 8 connections and prints MB/s alongside a
reset count for each. Set `DOWNLOAD_WORKERS` to whichever row wins:

- **scales up** with workers — use the fastest value, `8` or higher
- **flat** — the ceiling is your line or your account; leave it at `1`
- **drops, with resets climbing** — a network problem. Leave it at `1`; no
  setting here can fix a link that keeps dropping connections

`1` disables the parallel path entirely and uses Pyrogram's own downloader.

Both paths log their throughput on completion, so a normal run tells you what
you actually got:

```
Fast download finished: 622.0 MB over 8 connections in 71.3s = 8.73 MB/s
Sequential download finished: 622.0 MB in 475.1s = 1.31 MB/s
```

Files under 2 MB and any file Telegram serves via a CDN redirect automatically
use the normal single-connection path, as does anything the parallel path fails
on, so a bad setting degrades speed rather than breaking downloads.

Concurrency settings, which trade FloodWait risk for batch throughput:

```env
MAX_CONCURRENT_DOWNLOADS=1
BATCH_SIZE=1
FLOOD_WAIT_DELAY=5
````

Open connections are `DOWNLOAD_WORKERS × MAX_CONCURRENT_DOWNLOADS`. Keep
`MAX_CONCURRENT_DOWNLOADS=1` while `DOWNLOAD_WORKERS` is high — the parallel
downloader already saturates your line on a single file, and downloading two
files at once on top of that mostly buys dropped sockets. If you see repeated
`ConnectionResetError: Connection lost`, that product is too large.

Optional auto-forward setting:

```env
FORWARD_CHAT_ID=
```

To enable auto-forwarding, set `FORWARD_CHAT_ID` to a target channel/group chat ID or username.

Example:

```env
FORWARD_CHAT_ID=-1001234567890
```

Leave it empty to disable auto-forwarding.

### 2.3 Auto-forward requirements

If you enable `FORWARD_CHAT_ID`:

* The bot must be added to that target channel/group
* The bot must have permission to send messages/media there
* If the target chat is invalid or the bot has no permission, the bot will warn you and still send the downloaded content to your private chat normally

### 3) Start the bot

### Foreground (see logs in terminal)
```
docker compose up --build --remove-orphans
```

### Background (recommended)
```
docker compose up -d --build --remove-orphans
```

View logs:
```
docker compose logs -f
```

---

## 4) Use the bot (Telegram chat)

**Single post:**
```
/dl https://t.me/c/123456789/10
```

**Batch range:**
```
/bdl https://t.me/c/123456789/3 https://t.me/c/123456789/2598
```

**Single story:**
```
/dls https://t.me/username/s/12
```

**Batch story range:**
```
/bdls https://t.me/username/s/10 https://t.me/username/s/25
```

> 📌 Stories are only visible for 24 hours unless pinned. The user session
> must be able to view the story (follow the user / be in the channel).

**Stop current work:**
```
/killall
```

**Clean leftover temp files:**
```
/cleanup
```

**Stats / Logs:**
```
/stats
/logs
```

---

## 5) Stop the bot

```
docker compose down
```

---

## 6) Update the code

```
git pull
docker compose up -d --build --remove-orphans
```

---

## 7) Troubleshooting

### FloodWait / rate limits
Telegram will ask you to wait (e.g., “wait 2500 seconds”).

**What to do:**
- Stop the bot: `docker compose down`
- Wait out the cooldown
- Restart

**Prevent it:**
- Lower `DOWNLOAD_WORKERS` (try `4`, then `1` to rule it out as the cause)
- Set `MAX_CONCURRENT_DOWNLOADS=1` and `BATCH_SIZE=1`
- Raise `FLOOD_WAIT_DELAY` back to `10`
- Run smaller ranges (e.g., 1–300, then 301–600)

---

## 8) Where files are stored

- Downloads are created **inside the container** at `downloads/`.
- Because the repo is mounted, these files appear on your laptop under:
  ```
  RestrictedContentDL/downloads/
  ```
- They are **usually deleted automatically** after upload.
- If leftovers remain, run `/cleanup`.

---

## 9) Quick restart checklist

1) `docker compose down`
2) `docker compose up -d --build --remove-orphans`
3) `docker compose logs -f`

