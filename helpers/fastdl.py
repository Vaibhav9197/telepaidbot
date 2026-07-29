# Parallel-connection downloader for Telegram media.
#
# Pyrogram's Client.get_file walks a file in 1 MiB chunks over a single media
# session, awaiting each upload.getFile before asking for the next one. That
# makes throughput a function of per-connection speed and round-trip latency,
# which is why a naive bot sits at 1-3 MB/s while official clients saturate the
# line. Telegram throttles per connection rather than per account, so splitting
# one file across N media sessions to the same DC multiplies throughput without
# Premium.
#
# That premise holds only on a link that keeps those connections alive. Where
# connections are being reset, each reset costs a retry and more workers make
# downloads slower -- measured at 0.44 MB/s on 4 workers against 1.31 MB/s on
# one. Run benchmark_speed.py before trusting a DOWNLOAD_WORKERS value;
# DOWNLOAD_WORKERS=1 skips this module entirely.
#
# Any failure here raises FastDownloadUnavailable so the caller can fall back to
# Pyrogram's own downloader, which handles the cases this module deliberately
# skips (CDN redirects, chat photos, unknown sizes).

import os
import shutil
import asyncio
import inspect
from time import monotonic
from typing import Callable, Optional

from pyrogram import raw
from pyrogram.errors import FloodWait
from pyrogram.file_id import FileId, FileType

from logger import LOGGER

# MTProto rejects an upload.getFile whose range crosses a 1 MiB boundary, so a
# single request can never pull more than this.
CHUNK_SIZE = 1024 * 1024

# Below this, connection setup costs more than the parallelism buys back.
MIN_PARALLEL_SIZE = 2 * 1024 * 1024

# Seconds between progress edits. Left unthrottled, N workers each reporting per
# chunk would earn a FloodWait on editMessageText instead of a faster download.
PROGRESS_INTERVAL = 5

# Session.invoke retries internally (10 times, 15s apart by default) before
# raising, and fetch_chunk then retries on top of that. Left at the defaults the
# two multiply: 4 x 10 invokes is ~11 minutes spent on a single chunk, after
# which the whole file restarts on the sequential path anyway. Cap the inner
# budget so an unreachable chunk gives up in tens of seconds instead.
CHUNK_INVOKE_RETRIES = 2
CHUNK_INVOKE_TIMEOUT = 15

MEDIA_ATTRS = (
    "document",
    "video",
    "audio",
    "animation",
    "voice",
    "video_note",
    "sticker",
    "photo",
)


# How hard to try moving a finished download into place. Cloud-sync clients and
# antivirus scanners routinely hold a brand-new file open for a moment, and on
# Windows that blocks the rename outright.
FINALIZE_ATTEMPTS = 6
FINALIZE_INITIAL_DELAY = 0.25


# Retry counters, so a slow download can be attributed rather than guessed at.
# Parallel connections only pay off on a link that keeps them alive; if these
# climb, more workers make things worse, not better.
RETRY_STATS = {"flood": 0, "connection": 0}


def reset_retry_stats() -> None:
    RETRY_STATS["flood"] = 0
    RETRY_STATS["connection"] = 0


class FastDownloadUnavailable(Exception):
    """This file can't take the parallel path; the caller should fall back."""


async def finalize(temp_path: str, file_path: str) -> None:
    """Move a completed download into its final name.

    On Windows a freshly written file is often still held briefly by OneDrive or
    an antivirus scanner, which surfaces as WinError 32. Every byte is already on
    disk by this point, so wait the lock out and, failing that, copy the bytes
    across -- re-downloading a file we already have is never the right answer.
    """
    delay = FINALIZE_INITIAL_DELAY

    for attempt in range(FINALIZE_ATTEMPTS):
        try:
            os.replace(temp_path, file_path)
            return
        except OSError as e:
            if attempt == FINALIZE_ATTEMPTS - 1:
                LOGGER(__name__).warning(
                    f"Rename still blocked after {FINALIZE_ATTEMPTS} attempts "
                    f"({e.strerror}); copying instead"
                )
                break
            LOGGER(__name__).info(
                f"Rename blocked ({e.strerror}); retrying in {delay:.2f}s"
            )
            await asyncio.sleep(delay)
            delay *= 2

    # A copy only needs read access, so it can still succeed while whatever holds
    # the file is blocking the delete half of a rename.
    shutil.copyfile(temp_path, file_path)
    try:
        os.remove(temp_path)
    except OSError as e:
        LOGGER(__name__).warning(f"Could not remove {temp_path} after copy: {e}")


async def run_workers(sessions, worker) -> None:
    """Run one worker per session, making sure none outlive a failure.

    asyncio.gather re-raises the first exception but leaves its siblings running.
    Those orphans would keep writing into a file handle the caller is about to
    close, so cancel them before letting the error escape.
    """
    tasks = [asyncio.ensure_future(worker(session)) for session in sessions]

    try:
        await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


def extract_media(message):
    for attr in MEDIA_ATTRS:
        media = getattr(message, attr, None)
        if media is not None:
            return media
    return None


def build_location(file_id: FileId):
    if file_id.file_type == FileType.CHAT_PHOTO:
        raise FastDownloadUnavailable("chat photos are too small to parallelise")

    if file_id.file_type == FileType.PHOTO:
        return raw.types.InputPhotoFileLocation(
            id=file_id.media_id,
            access_hash=file_id.access_hash,
            file_reference=file_id.file_reference,
            thumb_size=file_id.thumbnail_size,
        )

    return raw.types.InputDocumentFileLocation(
        id=file_id.media_id,
        access_hash=file_id.access_hash,
        file_reference=file_id.file_reference,
        thumb_size=file_id.thumbnail_size,
    )


async def open_sessions(client, dc_id: int, count: int) -> list:
    # Warm the cached media session first. When the file lives on another DC this
    # is the call that performs the auth export/import handshake, and the
    # temporary sessions below reuse its auth key -- repeating the handshake once
    # per connection is a quick way to earn a FloodWait on auth.ExportAuthorization.
    await client.get_session(dc_id, is_media=True)

    sessions = []
    try:
        for _ in range(count):
            sessions.append(
                await client.get_session(
                    dc_id,
                    is_media=True,
                    temporary=True,
                    export_authorization=False,
                )
            )
    except Exception:
        await close_sessions(sessions)
        raise

    return sessions


async def close_sessions(sessions) -> None:
    for session in sessions:
        try:
            await session.stop()
        except Exception as e:
            LOGGER(__name__).warning(f"Failed to stop media session: {e}")


async def fetch_chunk(session, location, offset: int, attempts: int = 3) -> bytes:
    for attempt in range(attempts):
        try:
            result = await session.invoke(
                raw.functions.upload.GetFile(
                    location=location,
                    offset=offset,
                    limit=CHUNK_SIZE,
                ),
                retries=CHUNK_INVOKE_RETRIES,
                timeout=CHUNK_INVOKE_TIMEOUT,
                sleep_threshold=30,
            )
        except FloodWait as e:
            wait_s = int(getattr(e, "value", 0) or 0)
            RETRY_STATS["flood"] += 1
            if attempt == attempts - 1:
                raise
            LOGGER(__name__).warning(
                f"FloodWait fetching chunk at offset {offset}: {wait_s}s"
            )
            await asyncio.sleep(wait_s + 1)
            continue
        except (OSError, asyncio.TimeoutError) as e:
            RETRY_STATS["connection"] += 1
            # Holding many connections open makes the occasional reset routine.
            # Retry the chunk instead of losing the whole file to one dropped
            # socket; Pyrogram reconnects the session underneath us.
            if attempt == attempts - 1:
                raise
            LOGGER(__name__).warning(
                f"Connection error on chunk at offset {offset} "
                f"({type(e).__name__}: {e}); retrying"
            )
            await asyncio.sleep(1 + attempt)
            continue

        if isinstance(result, raw.types.upload.File):
            return result.bytes

        # upload.FileCdnRedirect needs the CDN decrypt-and-verify dance, which
        # Pyrogram already implements. Hand the file back rather than reimplement it.
        raise FastDownloadUnavailable(
            f"unsupported getFile response: {type(result).__name__}"
        )

    raise FastDownloadUnavailable(f"chunk at offset {offset} exhausted its retries")


async def fast_download(
    client,
    message,
    file_path: str,
    workers: int = 8,
    progress: Optional[Callable] = None,
    progress_args: tuple = (),
) -> str:
    """Download one message's media over `workers` parallel media sessions.

    Returns the final path. Raises FastDownloadUnavailable when the caller should
    retry through Pyrogram's sequential downloader instead.
    """
    media = extract_media(message)
    if media is None:
        raise FastDownloadUnavailable("message carries no downloadable media")

    file_id_str = getattr(media, "file_id", None)
    if not file_id_str:
        raise FastDownloadUnavailable("media has no file_id")

    file_size = getattr(media, "file_size", 0) or 0
    if file_size <= 0:
        raise FastDownloadUnavailable("media has no declared file_size")

    if file_size < MIN_PARALLEL_SIZE:
        raise FastDownloadUnavailable("file is small enough that one connection wins")

    file_id = FileId.decode(file_id_str)
    location = build_location(file_id)

    total_chunks = (file_size + CHUNK_SIZE - 1) // CHUNK_SIZE
    workers = max(1, min(workers, total_chunks))

    parent = os.path.dirname(file_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    temp_path = file_path + ".temp"

    sessions = await open_sessions(client, file_id.dc_id, workers)

    started = monotonic()
    next_chunk = 0
    downloaded = 0
    last_report = started
    chunk_lock = asyncio.Lock()

    async def report(force: bool = False) -> None:
        nonlocal last_report
        if progress is None:
            return
        now = monotonic()
        if not force and now - last_report < PROGRESS_INTERVAL:
            return
        last_report = now
        try:
            result = progress(min(downloaded, file_size), file_size, *progress_args)
            if inspect.isawaitable(result):
                await result
        except Exception as e:
            LOGGER(__name__).warning(f"Progress callback failed: {e}")

    try:
        with open(temp_path, "wb") as fh:
            # Preallocate so every worker can seek straight to its own offset.
            fh.truncate(file_size)

            async def worker(session):
                nonlocal next_chunk, downloaded

                while True:
                    async with chunk_lock:
                        index = next_chunk
                        if index >= total_chunks:
                            return
                        next_chunk += 1

                    offset = index * CHUNK_SIZE
                    chunk = await fetch_chunk(session, location, offset)
                    if not chunk:
                        continue

                    # No await between the seek and the write, so the event loop
                    # cannot interleave another worker's seek in between.
                    fh.seek(offset)
                    fh.write(chunk)

                    downloaded += len(chunk)
                    await report()

            await run_workers(sessions, worker)

        actual_size = os.path.getsize(temp_path)
        if actual_size != file_size:
            raise FastDownloadUnavailable(
                f"size mismatch: wrote {actual_size} of {file_size} bytes"
            )

        await finalize(temp_path, file_path)
        await report(force=True)

        elapsed = max(monotonic() - started, 1e-6)
        LOGGER(__name__).info(
            f"Fast download finished: {file_size / (1024 * 1024):.1f} MB over "
            f"{workers} connections in {elapsed:.1f}s = "
            f"{file_size / elapsed / (1024 * 1024):.2f} MB/s ({file_path})"
        )
        return file_path

    except FastDownloadUnavailable:
        _discard(temp_path)
        raise
    except Exception as e:
        _discard(temp_path)
        raise FastDownloadUnavailable(f"parallel download failed: {e}") from e
    finally:
        await close_sessions(sessions)


def _discard(path: str) -> None:
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError as e:
        LOGGER(__name__).warning(f"Could not remove partial download {path}: {e}")
