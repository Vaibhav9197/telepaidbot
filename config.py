# Copyright (C) @TheSmartBisnu
# Channel: https://t.me/itsSmartDev

from os import getenv
from time import time
from dotenv import load_dotenv

try:
    load_dotenv("config.env.local")
    load_dotenv("config.env")
except Exception:
    pass

if not getenv("API_ID") or not getenv("API_ID").strip().isdigit():
    print("Error: API_ID must be set to a valid numeric Telegram API ID")
    exit(1)

if not getenv("API_HASH") or len(getenv("API_HASH").strip()) < 10:
    print("Error: API_HASH must be set to a valid Telegram API hash")
    exit(1)

if not getenv("BOT_TOKEN") or not getenv("BOT_TOKEN").count(":") == 1:
    print("Error: BOT_TOKEN must be in format '123456:abcdefghijklmnopqrstuvwxyz'")
    exit(1)

if (
    not getenv("SESSION_STRING")
    or getenv("SESSION_STRING") == "xxxxxxxxxxxxxxxxxxxxxxx"
    or getenv("SESSION_STRING") == "your_session_string_here"
):
    print("Error: SESSION_STRING must be set with a valid string")
    exit(1)


# Pyrogram setup
class PyroConf(object):
    API_ID = int(getenv("API_ID"))
    API_HASH = getenv("API_HASH")
    BOT_TOKEN = getenv("BOT_TOKEN")
    SESSION_STRING = getenv("SESSION_STRING")
    BOT_START_TIME = time()

    MAX_CONCURRENT_DOWNLOADS = int(getenv("MAX_CONCURRENT_DOWNLOADS", "1"))
    FLOOD_WAIT_DELAY = int(getenv("FLOOD_WAIT_DELAY", "10"))

    # Uploads are gated separately from downloads so a finished download can hand
    # off and let the next file start while the previous one is still going out.
    # A download and an upload use opposite directions of the link, so running
    # one of each costs neither much speed.
    MAX_CONCURRENT_UPLOADS = max(1, min(int(getenv("MAX_CONCURRENT_UPLOADS", "1")), 8))

    # Items in flight at once, counting anything downloading, waiting to upload,
    # or uploading. At the default of 2 that is exactly one download alongside
    # one upload. Each item holds a file, so peak disk use is about
    # PIPELINE_DEPTH x file size.
    PIPELINE_DEPTH = max(1, min(int(getenv("PIPELINE_DEPTH", "2")), 16))

    # Free space kept in reserve before a download is allowed to begin.
    DISK_MIN_FREE_GB = max(0, int(getenv("DISK_MIN_FREE_GB", "2")))

    # Parallel media sessions used per file. Telegram throttles per connection,
    # so more connections usually mean more speed -- but only on a link that
    # keeps them alive, since every dropped connection costs a retry. Measure
    # with benchmark_speed.py before raising this; 1 disables the parallel path
    # and falls back to Pyrogram's sequential downloader.
    DOWNLOAD_WORKERS = max(1, min(int(getenv("DOWNLOAD_WORKERS", "8")), 16))

    # Concurrent transfers Pyrogram itself will allow, which also gates uploads
    # back to Telegram.
    MAX_CONCURRENT_TRANSMISSIONS = max(
        1, min(int(getenv("MAX_CONCURRENT_TRANSMISSIONS", "4")), 16)
    )

    FORWARD_CHAT_ID = getenv("FORWARD_CHAT_ID", "").strip() or None
