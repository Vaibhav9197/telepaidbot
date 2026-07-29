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
| [`/stats`](#stats) | Uptime, disk, memory, CPU, network totals |
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
✅ Batch Process Complete!
━━━━━━━━━━━━━━━━━━━
📥 Downloaded : 18 post(s)
⏭️ Skipped    : 2 (no content)
❌ Failed     : 1 error(s)
```

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
order. You get a downloaded / failed summary at the end.

## `/stats`

Current status of the machine the bot runs on:

- Bot uptime
- Total, used and free disk space
- Memory used by the bot process
- Total bytes uploaded and downloaded since boot
- CPU, RAM and disk percentages

Handy for checking free space before starting a large batch.

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
