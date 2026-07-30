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
#   python benchmark_speed.py --upload            (measure the upload leg)
#
# Upload mode sends throwaway bytes with Client.save_file, which uploads to
# Telegram without posting a message anywhere, and reports MB/s for both the
# bot and the user client. Uploads always use one connection and 512 KB parts
# (Pyrogram's save_file), so there is no worker count to sweep -- the useful
# comparison is bot vs user, and upload against download.
#
# Stop the bot first, or its own transfers will skew every number.

import asyncio
import os
import sys
import tempfile
from time import monotonic

from pyrogram import Client

from config import PyroConf
from helpers.fastdl import (
    CHUNK_SIZE,
    RETRY_STATS,
    build_location,
    extract_media,
    fetch_chunk,
    open_sessions,
    close_sessions,
    reset_retry_stats,
)
from helpers.msg import getChatMsgID
from pyrogram.file_id import FileId

MB = 1024 * 1024

# Below this the run measures connection setup rather than throughput. Each
# worker holds one 1 MiB chunk at a time, so a 7 MB file gives eight workers a
# single round trip each -- enough for latency jitter to reorder the whole table
# between runs, which is exactly how a 1-worker setting got justified once.
MIN_USEFUL_SAMPLE_MB = 32


def silence_teardown_noise() -> None:
    """Suppress the finalizer race that prints a traceback after the results.

    A StreamWriter that outlives the event loop tries to close itself during
    interpreter shutdown, and close() schedules work on a loop that is already
    gone. Python cannot propagate an exception out of __del__, so it prints
    "Exception ignored in: StreamWriter.__del__" and carries on -- the script
    still exits 0 with correct numbers, but it reads as a crash.

    This is suppression, not a fix. The orphaned writer lives inside pyrogram:
    TCP.close returns early when a writer is already closing and leaves the
    reference in place, so nothing this script can reach still owns it by the
    time it becomes collectable. Waiting does not help (the finalizer has not
    run yet) and neither does gc.collect (the client still holds a reference).

    Matched narrowly -- this one exception type, this one message -- so any
    other unraisable error still gets printed.
    """
    previous = sys.unraisablehook

    def hook(unraisable):
        exc = unraisable.exc_value
        if isinstance(exc, RuntimeError) and "Event loop is closed" in str(exc):
            return
        previous(unraisable)

    sys.unraisablehook = hook


async def shutdown(client) -> None:
    """Stop a client and give its transports a tick to finish closing."""
    try:
        await client.stop()
    except Exception as e:
        print(f"(client did not stop cleanly: {type(e).__name__}: {e})")
    await asyncio.sleep(0.25)


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
    reset_retry_stats()

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
    return (
        fetched / elapsed / MB,
        fetched / MB,
        elapsed,
        workers,
        dict(RETRY_STATS),
    )


async def measure_upload(client, label, sample_mb):
    """Upload throwaway bytes via save_file; nothing is posted anywhere."""
    path = os.path.join(tempfile.gettempdir(), f"_upbench_{label}.bin")
    block = os.urandom(MB)
    with open(path, "wb") as f:
        for _ in range(sample_mb):
            f.write(block)

    try:
        started = monotonic()
        await client.save_file(path)
        elapsed = max(monotonic() - started, 1e-6)
        return sample_mb / elapsed, elapsed
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


async def run_upload_benchmark(sample_mb):
    print(f"Uploading {sample_mb} MB per client with save_file "
          f"(nothing is posted).\n")
    print(f"{'client':>8}  {'MB/s':>8}  detail")
    print("-" * 40)

    clients = {
        "bot": Client(
            "bench_bot",
            api_id=PyroConf.API_ID,
            api_hash=PyroConf.API_HASH,
            bot_token=PyroConf.BOT_TOKEN,
            sleep_threshold=30,
            in_memory=True,
        ),
        "user": Client(
            "bench_user_up",
            api_id=PyroConf.API_ID,
            api_hash=PyroConf.API_HASH,
            session_string=PyroConf.SESSION_STRING,
            sleep_threshold=30,
            in_memory=True,
        ),
    }

    for label, client in clients.items():
        try:
            await client.start()
        except Exception as e:
            print(f"{label:>8}  {'ERROR':>8}  could not start: {e}")
            continue
        try:
            speed, elapsed = await measure_upload(client, label, sample_mb)
            print(f"{label:>8}  {speed:>8.2f}  {sample_mb} MB in {elapsed:.1f}s")
        except Exception as e:
            print(f"{label:>8}  {'ERROR':>8}  {type(e).__name__}: {e}")
        finally:
            await shutdown(client)

    print()
    print("Uploads use ONE connection and 512 KB parts, so unlike downloads")
    print("there is no parallelism to tune here. Compare this against your")
    print("download MB/s: most home links are asymmetric, and a slower upload")
    print("is usually the plan rather than anything the bot is doing.")
    print("If the bot row is much slower than the user row, uploading through")
    print("the user client instead is worth considering.")


async def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 1

    if "--upload" in sys.argv:
        sample_mb = 16
        if "--mb" in sys.argv:
            sample_mb = int(sys.argv[sys.argv.index("--mb") + 1])
        await run_upload_benchmark(sample_mb)
        return 0

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

        # How much will actually be fetched, which is not what was asked for
        # when the file is smaller than the sample.
        actual_mb = min(sample_mb * MB, media.file_size) / MB
        if actual_mb < MIN_USEFUL_SAMPLE_MB:
            chunks = max(1, int(actual_mb))
            # Plain ASCII on purpose: the Windows console runs cp1252, and a
            # warning that raises UnicodeEncodeError takes down the run it was
            # supposed to caution about.
            print(f"WARNING: this file is too small to measure. Only {actual_mb:.0f} MB "
                  f"({chunks} chunk{'s' if chunks != 1 else ''}) will be fetched\n"
                  f"   per run, so the highest worker counts get one round trip "
                  f"each and the\n"
                  f"   numbers below are mostly connection setup and latency "
                  f"jitter -- they will\n"
                  f"   disagree from run to run and should not be used to pick "
                  f"DOWNLOAD_WORKERS.\n\n"
                  f"   Point this at a post of at least "
                  f"{MIN_USEFUL_SAMPLE_MB * len(worker_counts)} MB and pass "
                  f"--mb {MIN_USEFUL_SAMPLE_MB} or more.\n")

        print(f"{'workers':>8}  {'MB/s':>8}  {'vs 1 conn':>10}  "
              f"{'resets':>7}  {'floods':>7}  detail")
        print("-" * 72)

        baseline = None
        for n in worker_counts:
            try:
                speed, got, elapsed, used, stats = await measure(
                    user, message, n, sample_mb * MB
                )
            except Exception as e:
                print(f"{n:>8}  {'ERROR':>8}  {'':>10}  {'':>7}  {'':>7}  "
                      f"{type(e).__name__}: {e}")
                continue

            if baseline is None:
                baseline = speed
            ratio = speed / baseline if baseline else 0
            print(f"{used:>8}  {speed:>8.2f}  {ratio:>9.2f}x  "
                  f"{stats['connection']:>7}  {stats['flood']:>7}  "
                  f"{got:.0f} MB in {elapsed:.1f}s")

            # Give Telegram a moment between runs so one trial does not
            # penalise the next.
            await asyncio.sleep(2)

        print()
        print("Set DOWNLOAD_WORKERS to whichever row is fastest.")
        print()
        print("If MB/s barely rises with more workers, the ceiling is your line")
        print("or an account-level cap, and DOWNLOAD_WORKERS cannot help.")
        print("If MB/s DROPS as workers rise, check the resets column: parallel")
        print("connections only pay off on a link that keeps them alive, and a")
        print("connection being reset repeatedly is a network problem no setting")
        print("in this bot can fix. Retrying it is what costs the throughput.")
    finally:
        await shutdown(user)

    return 0


if __name__ == "__main__":
    silence_teardown_noise()
    sys.exit(asyncio.run(main()))
