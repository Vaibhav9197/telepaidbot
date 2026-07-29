# Measure how download throughput scales with parallel connections.
#
# Telegram throttles per connection, so the whole premise of helpers/fastdl.py is
# that N connections give roughly N times the speed. This script tests that claim
# against your actual account and line instead of assuming it.
#
# Fetches a fixed slice of one message's media at several worker counts and
# reports MB/s for each. Nothing is written to disk, so it measures the network
# alone, and it stops well short of downloading the whole file.
#
# Usage:
#   python benchmark_speed.py https://t.me/c/2040588991/1011
#   python benchmark_speed.py <url> --mb 32 --workers 1,4,8,16
#
# Stop the bot first, or its own transfers will skew every number.

import asyncio
import sys
from time import monotonic

from pyrogram import Client

from config import PyroConf
from helpers.fastdl import (
    CHUNK_SIZE,
    build_location,
    extract_media,
    fetch_chunk,
    open_sessions,
    close_sessions,
)
from helpers.msg import getChatMsgID
from pyrogram.file_id import FileId

MB = 1024 * 1024


async def measure(client, message, workers, sample_bytes):
    """Fetch `sample_bytes` using `workers` connections; return MB/s."""
    media = extract_media(message)
    file_id = FileId.decode(media.file_id)
    location = build_location(file_id)

    total_chunks = max(1, min(sample_bytes // CHUNK_SIZE,
                             (media.file_size + CHUNK_SIZE - 1) // CHUNK_SIZE))
    workers = max(1, min(workers, total_chunks))

    sessions = await open_sessions(client, file_id.dc_id, workers)
    next_chunk = 0
    fetched = 0
    lock = asyncio.Lock()

    async def worker(session):
        nonlocal next_chunk, fetched
        while True:
            async with lock:
                index = next_chunk
                if index >= total_chunks:
                    return
                next_chunk += 1
            chunk = await fetch_chunk(session, location, index * CHUNK_SIZE)
            fetched += len(chunk)          # discarded, never written to disk

    started = monotonic()
    try:
        await asyncio.gather(*(worker(s) for s in sessions))
    finally:
        await close_sessions(sessions)

    elapsed = max(monotonic() - started, 1e-6)
    return fetched / elapsed / MB, fetched / MB, elapsed, workers


async def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 1

    url = sys.argv[1].split("?", 1)[0]
    sample_mb = 16
    worker_counts = [1, 2, 4, 8]

    if "--mb" in sys.argv:
        sample_mb = int(sys.argv[sys.argv.index("--mb") + 1])
    if "--workers" in sys.argv:
        worker_counts = [int(w) for w in
                         sys.argv[sys.argv.index("--workers") + 1].split(",")]

    total_mb = sample_mb * len(worker_counts)
    print(f"Sampling {sample_mb} MB per configuration at "
          f"{worker_counts} workers ({total_mb} MB total).\n")

    user = Client(
        "bench_user",
        api_id=PyroConf.API_ID,
        api_hash=PyroConf.API_HASH,
        session_string=PyroConf.SESSION_STRING,
        max_concurrent_transmissions=max(worker_counts),
        sleep_threshold=30,
        in_memory=True,
    )
    await user.start()

    try:
        chat_id, message_id = getChatMsgID(url)
        message = await user.get_messages(chat_id=chat_id, message_ids=message_id)
        media = extract_media(message)
        if media is None:
            print("That message has no downloadable media.")
            return 1

        print(f"File: {media.file_size / MB:.1f} MB "
              f"on DC{FileId.decode(media.file_id).dc_id}\n")
        print(f"{'workers':>8}  {'MB/s':>8}  {'vs 1 conn':>10}  detail")
        print("-" * 52)

        baseline = None
        for n in worker_counts:
            try:
                speed, got, elapsed, used = await measure(
                    user, message, n, sample_mb * MB
                )
            except Exception as e:
                print(f"{n:>8}  {'ERROR':>8}  {'':>10}  {type(e).__name__}: {e}")
                continue

            if baseline is None:
                baseline = speed
            ratio = speed / baseline if baseline else 0
            print(f"{used:>8}  {speed:>8.2f}  {ratio:>9.2f}x  "
                  f"{got:.0f} MB in {elapsed:.1f}s")

            # Give Telegram a moment between runs so one trial does not
            # penalise the next.
            await asyncio.sleep(2)

        print()
        print("If MB/s barely rises with more workers, the limit is your line or")
        print("Telegram's per-account cap, and DOWNLOAD_WORKERS cannot help.")
        print("If it scales, set DOWNLOAD_WORKERS to the best value above.")
    finally:
        await user.stop()

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
