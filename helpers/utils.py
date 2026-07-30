# Copyright (C) @TheSmartBisnu
# Channel: https://t.me/itsSmartDev

import os
import errno
import asyncio
import urllib.request
from time import time, monotonic
from PIL import Image
from logger import LOGGER
from typing import Optional
from asyncio.subprocess import PIPE
from asyncio import create_subprocess_exec, create_subprocess_shell, wait_for

from pyleaves import Leaves
from pyrogram.parser import Parser
from pyrogram.utils import get_channel_id
from pyrogram.errors import FloodWait, BadRequest
from pyrogram.types import (
    InputMediaPhoto,
    InputMediaVideo,
    InputMediaDocument,
    InputMediaAudio,
    Voice,
)

from config import PyroConf

from helpers.files import (
    fileSizeLimit,
    cleanup_download,
    get_download_path
)

from helpers.msg import (
    get_raw_text,
    get_file_name
)

from helpers.fastdl import (
    fast_download,
    FastDownloadUnavailable
)

from helpers import limits

# How many times to restart a download the network killed off. A flapping link
# can stay down for a minute or two, so wait a little longer after each attempt
# rather than burning all three retries inside the same outage.
DOWNLOAD_ATTEMPTS = 3
DOWNLOAD_RETRY_DELAY = 10

# Windows reports a dropped link through several distinct codes, and the
# exception class alone does not separate them from a disk problem: 64 and 10054
# arrive as ConnectionResetError, but 1231/1232 (network unreachable) come
# through as a bare OSError, exactly like a full disk would. Match on the code so
# a genuine write failure is not retried three times and then blamed on the WiFi.
NETWORK_WINERRORS = frozenset({64, 121, 1231, 1232})
NETWORK_ERRNOS = frozenset(
    code
    for code in (
        getattr(errno, name, None)
        for name in (
            "ECONNRESET",
            "ECONNABORTED",
            "ECONNREFUSED",
            "EPIPE",
            "ENETDOWN",
            "ENETRESET",
            "ENETUNREACH",
            "EHOSTUNREACH",
            "ETIMEDOUT",
        )
    )
    if code is not None
)


# Throughput of the most recent transfer on each leg, so /speed can compare what
# the bot actually achieved against what the line is capable of. Without that
# comparison a number like "1.31 MB/s" reads as a bot problem, when on a 10 Mbps
# line it is the whole connection and no setting can improve on it.
LAST_SPEED = {"download": None, "upload": None}


def record_speed(leg: str, size_bytes: int, elapsed: float) -> float:
    """Store and return MB/s for a finished transfer."""
    mbps = size_bytes / max(elapsed, 1e-6) / (1024 * 1024)
    LAST_SPEED[leg] = mbps
    return mbps


def is_connection_error(e: BaseException) -> bool:
    """Whether this exception is the link dropping rather than a real failure.

    Pyrogram gives up on a stalled request with a bare TimeoutError, which says
    nothing about the cause, so treat that as transient too -- the request had
    already been retried ten times underneath before it surfaced.
    """
    if isinstance(e, (ConnectionError, asyncio.TimeoutError)):
        return True
    if isinstance(e, OSError):
        return (
            getattr(e, "winerror", None) in NETWORK_WINERRORS
            or e.errno in NETWORK_ERRNOS
        )
    return False


# Progress bar template
PROGRESS_BAR = """
Percentage: {percentage:.2f}% | {current}/{total}
Speed: {speed}/s
Estimated Time Left: {est_time} seconds
"""

async def cmd_exec(cmd, shell=False):
    if shell:
        proc = await create_subprocess_shell(cmd, stdout=PIPE, stderr=PIPE)
    else:
        proc = await create_subprocess_exec(*cmd, stdout=PIPE, stderr=PIPE)
    stdout, stderr = await proc.communicate()
    try:
        stdout = stdout.decode().strip()
    except Exception:
        stdout = "Unable to decode the response!"
    try:
        stderr = stderr.decode().strip()
    except Exception:
        stderr = "Unable to decode the error!"
    return stdout, stderr, proc.returncode


async def get_media_info(path):
    try:
        result = await cmd_exec([
            "ffprobe", "-hide_banner", "-loglevel", "error",
            "-print_format", "json", "-show_format", "-show_streams", path,
        ])
    except Exception as e:
        LOGGER(__name__).error(f"Get Media Info: {e}. File: {path}")
        return 0, None, None, None, None

    if result[0] and result[2] == 0:
        try:
            import json
            data = json.loads(result[0])

            fields = data.get("format", {})
            duration = round(float(fields.get("duration", 0)))

            tags = fields.get("tags", {})
            artist = tags.get("artist") or tags.get("ARTIST") or tags.get("Artist")
            title = tags.get("title") or tags.get("TITLE") or tags.get("Title")

            width = None
            height = None
            for stream in data.get("streams", []):
                if stream.get("codec_type") == "video":
                    width = stream.get("width")
                    height = stream.get("height")
                    break

            return duration, artist, title, width, height
        except Exception as e:
            LOGGER(__name__).error(f"Error parsing media info: {e}")
            return 0, None, None, None, None
    return 0, None, None, None, None


async def get_video_thumbnail(video_file, duration):
    os.makedirs("Assets", exist_ok=True)
    output = os.path.join("Assets", "video_thumb.jpg")

    if duration is None:
        duration = (await get_media_info(video_file))[0]
    if not duration:
        duration = 3
    duration //= 2

    if os.path.exists(output):
        try:
            os.remove(output)
        except:
            pass

    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-ss", str(duration), "-i", video_file,
        "-vframes", "1", "-q:v", "2",
        "-y", output,
    ]
    try:
        _, err, code = await wait_for(cmd_exec(cmd), timeout=60)
        if code != 0 or not os.path.exists(output):
            LOGGER(__name__).warning(f"Thumbnail generation failed: {err}")
            return None
    except Exception as e:
        LOGGER(__name__).warning(f"Thumbnail generation error: {e}")
        return None
    return output


# Generate progress bar for downloading/uploading
def progressArgs(action: str, progress_message, start_time):
    return (action, progress_message, start_time, PROGRESS_BAR, "▓", "░")


# Neutral endpoints for measuring the line itself. Deliberately not Telegram:
# the point is to establish what the connection can do at all, so that a slow
# transfer can be attributed to the line rather than to the bot or to Telegram.
SPEEDTEST_DOWN_URL = "https://speed.cloudflare.com/__down?bytes={bytes}"
SPEEDTEST_UP_URL = "https://speed.cloudflare.com/__up"
SPEEDTEST_DOWN_BYTES = 8 * 1024 * 1024
SPEEDTEST_UP_BYTES = 3 * 1024 * 1024
SPEEDTEST_TIMEOUT = 90


def _http_speedtest(url: str, payload: Optional[bytes] = None) -> float:
    """Blocking transfer against a neutral host; returns MB/s. Runs off-loop."""
    req = urllib.request.Request(
        url,
        data=payload,
        # Cloudflare refuses the default urllib agent outright.
        headers={"User-Agent": "Mozilla/5.0", "Content-Type": "application/octet-stream"},
        method="POST" if payload is not None else "GET",
    )
    moved = len(payload) if payload is not None else 0
    started = monotonic()
    with urllib.request.urlopen(req, timeout=SPEEDTEST_TIMEOUT) as resp:
        while payload is None:
            block = resp.read(65536)
            if not block:
                break
            moved += len(block)
        if payload is not None:
            resp.read()
    return moved / max(monotonic() - started, 1e-6) / (1024 * 1024)


async def measure_line_capacity() -> dict:
    """Measure what the connection itself can do, in both directions.

    urllib is blocking, so each leg goes to a thread rather than stalling the
    event loop and starving whatever transfer is already running.
    """
    result = {"down": None, "up": None}
    try:
        result["down"] = await asyncio.to_thread(
            _http_speedtest, SPEEDTEST_DOWN_URL.format(bytes=SPEEDTEST_DOWN_BYTES)
        )
    except Exception as e:
        LOGGER(__name__).warning(f"Download capacity probe failed: {e}")
    try:
        result["up"] = await asyncio.to_thread(
            _http_speedtest, SPEEDTEST_UP_URL, b"\0" * SPEEDTEST_UP_BYTES
        )
    except Exception as e:
        LOGGER(__name__).warning(f"Upload capacity probe failed: {e}")
    return result


def is_copyable(chat_message) -> bool:
    """Whether Telegram will let this post be copied between chats.

    Protection is set per message and per chat, and either one blocks a copy, so
    both have to be clear. Both attributes are optional in the schema and absent
    means "not protected", which is why this reads them defensively rather than
    trusting them to be present.
    """
    if getattr(chat_message, "has_protected_content", False):
        return False
    chat = getattr(chat_message, "chat", None)
    return not getattr(chat, "has_protected_content", False)


async def try_server_side_copy(user, bot, chat_message, forward_chat_id=None) -> bool:
    """Let Telegram move the post between chats instead of routing it through here.

    Downloading and re-uploading sends every byte over the local link twice. On a
    slow connection that is minutes per file, and it is pure waste whenever the
    source allows forwarding: Telegram can copy the post server-side, so the
    bytes never leave its infrastructure and the transfer is effectively instant.

    Only the user session can see the source chat, so it is the one that copies.
    That means the post arrives under the account's own name rather than the
    bot's -- the one visible difference, and the reason SERVER_SIDE_COPY exists.

    Returns True if the post was delivered. Anything that says otherwise returns
    False, and the caller falls back to the download path having sent nothing.
    """
    bot_id = getattr(getattr(bot, "me", None), "id", None)
    if bot_id is None:
        return False

    is_album = bool(chat_message.media_group_id)
    copy = user.copy_media_group if is_album else user.copy_message

    # The forward chat goes first, deliberately. It is the copy most likely to be
    # refused -- the account may not be a member -- and finding that out before
    # anything has been delivered is what lets us fall back cleanly. Reversed, a
    # refusal here would mean re-downloading a post the user already has.
    targets = ([forward_chat_id] if forward_chat_id else []) + [bot_id]
    delivered = False

    for target in targets:
        try:
            await copy(
                chat_id=target,
                from_chat_id=chat_message.chat.id,
                message_id=chat_message.id,
            )
            delivered = True
        except FloodWait as e:
            wait_s = int(getattr(e, "value", 0) or 0)
            LOGGER(__name__).warning(f"FloodWait during server-side copy: {wait_s}s")
            if not delivered:
                return False
        except Exception as e:
            # ChatForwardsRestricted and friends are only decidable by the server,
            # so a refusal here is expected rather than exceptional.
            LOGGER(__name__).info(
                f"Server-side copy to {target} refused ({type(e).__name__}: {e})"
            )
            if not delivered:
                return False

    if delivered:
        LOGGER(__name__).info(
            f"Copied {'album' if is_album else 'post'} server-side from "
            f"{chat_message.chat.id}/{chat_message.id} -- no bytes transferred locally"
        )
    return delivered


async def download_with_fallback(
    message,
    file_path: Optional[str],
    progress_message,
    start_time,
    label: str = "📥 Downloading Progress",
):
    """Download media as fast as the account allows.

    Tries the parallel-connection downloader first, then falls back to
    Pyrogram's sequential one for anything it declines to handle (CDN
    redirects, tiny files, unknown sizes) or fails on.
    """
    progress = Leaves.progress_for_pyrogram
    args = progressArgs(label, progress_message, start_time)
    client = getattr(message, "_client", None)

    if client is not None and file_path and PyroConf.DOWNLOAD_WORKERS > 1:
        try:
            started = monotonic()
            result = await fast_download(
                client,
                message,
                file_path,
                workers=PyroConf.DOWNLOAD_WORKERS,
                progress=progress,
                progress_args=args,
            )
            # fastdl logs its own throughput but cannot record it here without
            # importing this module back, so the timing is repeated on this side.
            if result and os.path.exists(result):
                record_speed(
                    "download", os.path.getsize(result), monotonic() - started
                )
            return result
        except FastDownloadUnavailable as e:
            LOGGER(__name__).info(f"Using sequential download instead: {e}")
        except FloodWait as e:
            wait_s = int(getattr(e, "value", 0) or 0)
            LOGGER(__name__).warning(f"FloodWait during fast download: {wait_s}s")
            if wait_s > 0:
                await asyncio.sleep(wait_s + 1)
        except (OSError, asyncio.TimeoutError) as e:
            # fast_download turns its own failures into FastDownloadUnavailable,
            # but opening the media sessions happens before that guard, so a link
            # that is down at that moment escapes raw. Falling through costs one
            # sequential attempt; letting it out skips the fallback altogether.
            if not is_connection_error(e):
                raise
            LOGGER(__name__).warning(
                f"Parallel download could not start ({type(e).__name__}: {e}); "
                "using sequential download instead"
            )

    kwargs = {"progress": progress, "progress_args": args}
    if file_path:
        kwargs["file_name"] = file_path

    for attempt in range(DOWNLOAD_ATTEMPTS):
        last = attempt == DOWNLOAD_ATTEMPTS - 1
        try:
            started = monotonic()
            result = await message.download(**kwargs)

            # Log the sequential path's throughput too, so the two are directly
            # comparable when tuning DOWNLOAD_WORKERS.
            if result and os.path.exists(result):
                size = os.path.getsize(result)
                elapsed = max(monotonic() - started, 1e-6)
                mbps = record_speed("download", size, elapsed)
                LOGGER(__name__).info(
                    f"Sequential download finished: {size / (1024 * 1024):.1f} MB "
                    f"in {elapsed:.1f}s = {mbps:.2f} MB/s"
                )
            return result
        except FloodWait as e:
            wait_s = int(getattr(e, "value", 0) or 0)
            LOGGER(__name__).warning(f"FloodWait while downloading media: {wait_s}s")
            if wait_s > 0 and not last:
                await asyncio.sleep(wait_s + 1)
                continue
            raise
        except (OSError, asyncio.TimeoutError) as e:
            # Pyrogram has already exhausted its own retries by this point, so a
            # link that flaps for a couple of minutes takes the whole file down
            # with it. Every byte has to come again from offset zero, but that
            # still beats reporting a failure the user has to re-queue by hand.
            if last or not is_connection_error(e):
                raise
            LOGGER(__name__).warning(
                f"Connection lost while downloading ({type(e).__name__}: {e}); "
                f"restarting download in {DOWNLOAD_RETRY_DELAY * (attempt + 1)}s "
                f"(attempt {attempt + 2} of {DOWNLOAD_ATTEMPTS})"
            )
            await asyncio.sleep(DOWNLOAD_RETRY_DELAY * (attempt + 1))
            continue

    return None


async def send_media(
    bot, message, media_path, media_type, caption, caption_entities,
    progress_message, start_time, forward_chat_id=None
):
    file_size = os.path.getsize(media_path)

    if not await fileSizeLimit(file_size, message, "upload"):
        return

    progress_args = progressArgs("📥 Uploading Progress", progress_message, start_time)
    LOGGER(__name__).info(f"Uploading media: {media_path} ({media_type})")

    sent_message = None

    async def _send_once(cap, ents):
        nonlocal sent_message
        if media_type == "photo":
            sent_message = await message.reply_photo(
                media_path,
                caption=cap,
                caption_entities=ents or None,
                progress=Leaves.progress_for_pyrogram,
                progress_args=progress_args,
            )
            return
        if media_type == "video":
            duration, _, _, width, height = await get_media_info(media_path)

            if not duration or duration == 0:
                duration = 0
                LOGGER(__name__).warning(f"Could not extract duration for {media_path}")

            if not width or not height:
                width = 640
                height = 480

            thumb = await get_video_thumbnail(media_path, duration)

            sent_message = await message.reply_video(
                media_path,
                duration=duration,
                width=width,
                height=height,
                thumb=thumb,
                caption=cap,
                caption_entities=ents or None,
                supports_streaming=True,
                progress=Leaves.progress_for_pyrogram,
                progress_args=progress_args,
            )
            if thumb:
                cleanup_download(thumb)
            return
        if media_type == "audio":
            duration, artist, title, _, _ = await get_media_info(media_path)
            sent_message = await message.reply_audio(
                media_path,
                duration=duration,
                performer=artist,
                title=title,
                caption=cap,
                caption_entities=ents or None,
                progress=Leaves.progress_for_pyrogram,
                progress_args=progress_args,
            )
            return
        if media_type == "document":
            sent_message = await message.reply_document(
                media_path,
                caption=cap,
                caption_entities=ents or None,
                progress=Leaves.progress_for_pyrogram,
                progress_args=progress_args,
            )

    cur_cap = caption or ""
    cur_ents = caption_entities or []
    upload_started = monotonic()
    for attempt in range(2):
        try:
            await _send_once(cur_cap, cur_ents)
            elapsed = max(monotonic() - upload_started, 1e-6)
            mbps = record_speed("upload", file_size, elapsed)
            LOGGER(__name__).info(
                f"Upload finished: {file_size / (1024 * 1024):.1f} MB in "
                f"{elapsed:.1f}s = {mbps:.2f} MB/s"
            )
            break
        except FloodWait as e:
            wait_s = int(getattr(e, "value", 0) or 0)
            LOGGER(__name__).warning(f"FloodWait while uploading media: {wait_s}s")
            if wait_s > 0 and attempt == 0:
                await asyncio.sleep(wait_s + 1)
                continue
            raise
        except BadRequest as e:
            if "ENTITY_TEXT_INVALID" in str(e) and attempt == 0:
                LOGGER(__name__).warning(f"ENTITY_TEXT_INVALID in caption entities, retrying without entities: {e}")
                cur_ents = []
                continue
            raise

    if forward_chat_id and sent_message:
        for attempt in range(2):
            try:
                await bot.copy_message(
                    chat_id=forward_chat_id,
                    from_chat_id=sent_message.chat.id,
                    message_id=sent_message.id,
                )
                LOGGER(__name__).info(f"Copied media to chat: {forward_chat_id}")
                break
            except FloodWait as e:
                wait_s = int(getattr(e, "value", 0) or 0)
                LOGGER(__name__).warning(f"FloodWait while copying media: {wait_s}s")
                if wait_s > 0 and attempt == 0:
                    await asyncio.sleep(wait_s + 1)
                    continue
                LOGGER(__name__).error(f"Failed to copy media after retry: FloodWait")
            except Exception as e:
                LOGGER(__name__).error(f"Failed to copy media to {forward_chat_id}: {e}")
                break


async def download_single_media(msg, progress_message, start_time, file_path=None):
    try:
        media_path = await download_with_fallback(
            msg, file_path, progress_message, start_time
        )

        if not media_path:
            return ("error", None, None)

        raw_cap, raw_ents = get_raw_text(msg.caption, msg.caption_entities)

        if msg.photo:
            return ("success", media_path, InputMediaPhoto(media=media_path, caption=raw_cap, caption_entities=raw_ents or None))
        if msg.video:
            return ("success", media_path, InputMediaVideo(media=media_path, caption=raw_cap, caption_entities=raw_ents or None))
        if msg.document:
            return ("success", media_path, InputMediaDocument(media=media_path, caption=raw_cap, caption_entities=raw_ents or None))
        if msg.audio:
            return ("success", media_path, InputMediaAudio(media=media_path, caption=raw_cap, caption_entities=raw_ents or None))

    except FloodWait as e:
        wait_s = int(getattr(e, "value", 0) or 0)
        LOGGER(__name__).warning(f"FloodWait while downloading media: {wait_s}s")
        return ("error", None, None)
    except Exception as e:
        LOGGER(__name__).info(f"Error downloading media: {e}")
        return ("error", None, None)

    return ("skip", None, None)


async def processMediaGroup(chat_message, bot, message, forward_chat_id=None):
    media_group_messages = await chat_message.get_media_group()
    valid_media = []
    temp_paths = []
    invalid_paths = []

    start_time = time()
    progress_message = await message.reply("📥 Downloading media group...")
    LOGGER(__name__).info(
        f"Downloading media group with {len(media_group_messages)} items..."
    )

    download_tasks = []
    for msg in media_group_messages:
        if msg.photo or msg.video or msg.document or msg.audio:
            # An explicit path is what lets the parallel downloader take over;
            # without one it can't place the chunks itself. item_id keeps album
            # entries apart when Telegram gives them the same file_name.
            item_path = get_download_path(
                message.id, get_file_name(msg.id, msg), item_id=msg.id
            )
            download_tasks.append(
                download_single_media(msg, progress_message, start_time, item_path)
            )

    # Album items already download in parallel among themselves; the semaphore
    # keeps the group as a whole from overlapping another request's download.
    async with limits.download_semaphore:
        results = await asyncio.gather(*download_tasks, return_exceptions=True)

    for result in results:
        if isinstance(result, Exception):
            LOGGER(__name__).error(f"Download task failed: {result}")
            continue

        status, media_path, media_obj = result
        if status == "success" and media_path and media_obj:
            temp_paths.append(media_path)
            valid_media.append(media_obj)
        elif status == "error" and media_path:
            invalid_paths.append(media_path)

    LOGGER(__name__).info(f"Valid media count: {len(valid_media)}")

    # Cleanup lives in a finally spanning both outcomes. An album is several
    # files at once, and a FloodWait on any reply or delete on the way out would
    # otherwise leak the whole group. temp_paths is only ever appended alongside
    # valid_media, so this covers exactly what the per-branch cleanups did.
    try:
        if valid_media:
            sent_messages = []
            try:
                for attempt in range(3):
                    try:
                        async with limits.upload_semaphore:
                            sent_messages = await bot.send_media_group(chat_id=message.chat.id, media=valid_media)
                        await progress_message.delete()
                        break
                    except FloodWait as e:
                        wait_s = int(getattr(e, "value", 0) or 0)
                        LOGGER(__name__).warning(f"FloodWait while sending media group: {wait_s}s")
                        if wait_s > 0 and attempt < 2:
                            await asyncio.sleep(wait_s + 1)
                            continue
                        raise
                    except BadRequest as e:
                        if "ENTITY_TEXT_INVALID" in str(e) and attempt == 0:
                            LOGGER(__name__).warning(f"ENTITY_TEXT_INVALID in media group, retrying without caption entities: {e}")
                            for m in valid_media:
                                m.caption_entities = None
                            continue
                        raise
            except Exception:
                await message.reply(
                    "**❌ Failed to send media group, trying individual uploads**"
                )
                for media in valid_media:
                    try:
                        sent = None
                        if isinstance(media, InputMediaPhoto):
                            sent = await bot.send_photo(
                                chat_id=message.chat.id,
                                photo=media.media,
                                caption=media.caption,
                            )
                        elif isinstance(media, InputMediaVideo):
                            sent = await bot.send_video(
                                chat_id=message.chat.id,
                                video=media.media,
                                caption=media.caption,
                            )
                        elif isinstance(media, InputMediaDocument):
                            sent = await bot.send_document(
                                chat_id=message.chat.id,
                                document=media.media,
                                caption=media.caption,
                            )
                        elif isinstance(media, InputMediaAudio):
                            sent = await bot.send_audio(
                                chat_id=message.chat.id,
                                audio=media.media,
                                caption=media.caption,
                            )
                        elif isinstance(media, Voice):
                            sent = await bot.send_voice(
                                chat_id=message.chat.id,
                                voice=media.media,
                                caption=media.caption,
                            )
                        if sent:
                            sent_messages.append(sent)
                    except Exception as individual_e:
                        await message.reply(
                            f"Failed to upload individual media: {individual_e}"
                        )

                await progress_message.delete()

            if forward_chat_id and sent_messages:
                try:
                    msg_ids = [m.id for m in sent_messages if m]
                    if msg_ids:
                        source_chat_id = sent_messages[0].chat.id
                        for attempt in range(2):
                            try:
                                await bot.copy_media_group(
                                    chat_id=forward_chat_id,
                                    from_chat_id=source_chat_id,
                                    message_id=msg_ids[0],
                                )
                                LOGGER(__name__).info(f"Copied media group to chat: {forward_chat_id}")
                                break
                            except FloodWait as e:
                                wait_s = int(getattr(e, "value", 0) or 0)
                                LOGGER(__name__).warning(f"FloodWait while copying media group: {wait_s}s")
                                if wait_s > 0 and attempt == 0:
                                    await asyncio.sleep(wait_s + 1)
                                    continue
                                raise
                except Exception as e:
                    LOGGER(__name__).error(f"Failed to copy media group to {forward_chat_id}: {e}")

            return True

        await progress_message.delete()
        await message.reply("❌ No valid media found in the media group.")
        return False
    finally:
        for path in temp_paths + invalid_paths:
            cleanup_download(path)
