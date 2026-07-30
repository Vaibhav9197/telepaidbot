# Bot Commands

Everything you can send the bot in Telegram.

> **Private chat only.** Every command is registered with `filters.private`, so
> the bot does not respond in groups or channels.

| Command | What it does |
|---|---|
| [`/start`](#start) | Welcome message and a short introduction |
| [`/help`](#help) | Command list with examples, inside Telegram |
| [`/dl <url>`](#dl-url) | Download one post |
| [`/bdl <start> <end>`](#bdl-start_url-end_url) | Download a range of posts |
| [`/dls <url>`](#dls-url) | Download one story |
| [`/bdls <start> <end>`](#bdls-start_url-end_url) | Download a range of stories |
| [`/retry`](#retry) | Resume the last batch, re-running only what failed |
| [`/stats`](#stats) | Uptime, disk, memory, CPU, network totals |
| [`/speed`](#speed) | Your line's capacity next to what transfers achieved |
| [`/logs`](#logs) | Sends the log file to you |
| [`/killall`](#killall) | Cancels every running download and upload |
| [`/cleanup`](#cleanup) | Deletes leftover files from `downloads/` |

---

## `/start`

Welcome text with a quick summary of what the bot handles and a link to the
update channel. Nothing else happens.

## `/help`

The same command reference as this file, but inside Telegram so you do not have
to leave the chat.

## `/dl <url>`

Download a single post.

```
/dl https://t.me/mychannel/100
```

Handles photos, videos, audio, documents, voice notes, video notes, animations
and stickers. Depending on what the post contains:

- **Media** — downloaded and sent back to you with its original caption
- **Album** — the whole group is fetched and sent together, once
- **Text only** — the text is copied to you, formatting preserved
- **Poll** — not downloadable; the bot tells you so

**You can skip the command.** Pasting a post link on its own does exactly the
same thing. The same applies to story links.

Private channel links (`https://t.me/c/...`) work as long as your user session
is a member.

### Posts that skip the download entirely

If the source allows forwarding, the post is copied server-side: Telegram moves
it between chats internally, nothing is transferred over your connection, and it
arrives in seconds instead of the minutes a download plus a re-upload costs. A
600 MB video on a 10 Mbps line is roughly fifteen minutes saved.

Only the user session can see the source, so it is the account that performs the
copy — meaning these posts arrive under your own name rather than the bot's.
That is the one visible difference. Set `SERVER_SIDE_COPY = 0` in `config.env` if
you would rather everything came from the bot.

Posts with content protection enabled cannot be copied and always take the full
download path, unchanged.

## `/bdl <start_url> <end_url>`

Download a range of posts in one go.

```
/bdl https://t.me/mychannel/100 https://t.me/mychannel/120
```

Downloads posts 100 to 120 inclusive. Both links must:

- start with `https://t.me/`
- point at the **same** chat or channel
- be in ascending order — start ID no higher than end ID

Posts with nothing downloadable are skipped rather than treated as failures, and
an album is fetched once even when several IDs in the range belong to it. When
the range finishes you get a summary:

```
⚠️ Batch finished with failures
━━━━━━━━━━━━━━━━━━━
📥 Downloaded : 18 post(s)
⏭️ Skipped    : 2 (no content)
❌ Failed     : 1 error(s)

Send /retry to run the remaining 1 again. Everything already downloaded is skipped.
```

The header reads **✅ Batch Complete!** instead when nothing was left outstanding.

Progress is recorded per item as the batch runs, so a range interrupted by a
dropped connection, a crash or [`/killall`](#killall) can be picked up with
[`/retry`](#retry) rather than started over.

Use [`/killall`](#killall) to stop a batch part-way.

## `/dls <url>`

Download a single story.

```
/dls https://t.me/username/s/12
```

The link must look like `https://t.me/<username>/s/<story_id>`; anything else is
rejected with an explanation.

Your user session has to be able to see the story — follow the user or be in the
channel. Stories vanish after 24 hours unless the poster pinned them, so an old
story ID will fail even with the right access.

## `/bdls <start_url> <end_url>`

Download a range of stories.

```
/bdls https://t.me/username/s/10 https://t.me/username/s/25
```

Both links must be valid story links from the same user or channel, in ascending
order. You get a downloaded / failed summary at the end, and as with `/bdl` an
interrupted range can be resumed with [`/retry`](#retry).

## `/stats`

Current status of the machine the bot runs on:

- Bot uptime
- Total, used and free disk space
- Memory used by the bot process
- Total bytes uploaded and downloaded since boot
- CPU, RAM and disk percentages

Handy for checking free space before starting a large batch.

The disk figures describe the volume downloads are staged on (`DOWNLOAD_DIR`),
which is not necessarily the one the code is checked out to.

## `/retry`

Resumes the last batch, re-running only the items that did not get through.

```
/retry
```

Batches record every item's result as they go, so an interrupted run leaves a
resume point rather than a lost range:

```
🔄 Resuming batch from 2026-07-30T09:14:02
━━━━━━━━━━━━━━━━━━━
✅ Already done : 41 — will not be downloaded again
⏭️ Skipped      : 3 — cannot succeed, ignoring
🔄 Retrying     : 16 of 60
```

Anything already delivered is left alone — on a slow line, re-downloading 41
posts to recover 16 costs far more than the failures did.

Items are only retried if another attempt could plausibly work: a dropped
connection, a rate limit, a channel you had not joined yet. Posts that failed
for reasons no retry can fix — a poll, a file over the size limit, an expired
story, a malformed link — are recorded as skipped and stay that way, so `/retry`
cannot loop on them forever.

The resume point survives a restart of the bot, so a crash mid-batch is
recoverable. It is cleared automatically once the batch completes; `/retry` with
nothing outstanding just tells you so. Run it while a batch is still going and
it declines, rather than having two runs fight over the same items.

## `/speed`

Measures your internet connection against a neutral host — not Telegram — and
reports it next to the throughput the last download and upload actually got:

```
📶 Connection
➜ Line download: 1.25 MB/s (10.0 Mbps)
➜ Line upload: 1.36 MB/s (10.9 Mbps)

📊 Last transfer
➜ Last download: 1.31 MB/s — 105% of line
➜ Last upload: 1.28 MB/s — 94% of line
```

Use this before trying to tune anything. A transfer sitting near the line figure
means the bot is already moving bytes as fast as the connection allows, and no
setting will make it faster — the ceiling is the link, not the code. Bots that
appear much faster are almost always running on a server rather than a home
connection.

## `/logs`

Sends `logs.txt` back as a file attachment. Replies `Not exists` if no log file
has been written yet.

Useful when a download failed and you want to see why — the log records the
reason, the throughput of each transfer, and any rate limits hit.

## `/killall`

Cancels every download and upload currently in progress and reports how many
tasks it stopped:

```
Cancelled 2 running task(s).
```

Use it when a batch is running longer than you want or a transfer looks stuck.

Two things to know:

- It does **not** stop the bot itself — the bot stays up and keeps accepting
  commands.
- A cancelled download leaves a partial `.temp` file behind. Follow with
  [`/cleanup`](#cleanup) if space is tight.

Cancelling a batch keeps its resume point. The items you interrupted are
recorded as not-yet-attempted rather than failed, so [`/retry`](#retry) picks up
exactly where it stopped.

## `/cleanup`

Deletes everything under `downloads/` and reports what it freed:

```
🧹 Cleanup complete: removed 4 file(s), freed 1.86 GB
```

Says `no local downloads found` if there was nothing to remove.

Files are normally cleaned up automatically once a download has been sent to
you, so anything left over comes from a cancelled task or a bot that was killed
mid-transfer. Those leftovers still occupy disk and count against the free-space
check, so run this if downloads start refusing to begin for lack of space.
