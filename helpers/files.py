# Copyright (C) @TheSmartBisnu
# Channel: https://t.me/itsSmartDev

import os
import shutil
from typing import Optional

from config import PyroConf
from logger import LOGGER

SIZE_UNITS = ["B", "KB", "MB", "GB", "TB", "PB"]

# Staging root for in-flight downloads. Callers used to default to a relative
# "downloads", which resolved against the working directory and so landed inside
# the checkout -- and, for a checkout under a sync client, inside the synced tree.
# See PyroConf.DOWNLOAD_DIR for why that is expensive.
DOWNLOADS_ROOT = PyroConf.DOWNLOAD_DIR

# Where downloads landed before DOWNLOAD_DIR existed. Files staged there are
# still this bot's, and they are the ones most likely to be noticed, because
# they sit in the checkout rather than out of sight in %TEMP% -- so /cleanup
# reporting "no local downloads found" over the top of them reads as broken.
LEGACY_DOWNLOADS_ROOT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "downloads"
)


# Characters Windows refuses outright in a filename, plus the control range.
# Uploaders put all of these in file names, and Telegram passes them through
# verbatim, so whatever arrives has to be treated as untrusted text rather than
# as a usable path component.
ILLEGAL_CHARS = '<>:"/\\|?*'

# Device names that are still special no matter the extension: opening CON.mp4
# talks to the console, not to a file.
RESERVED_NAMES = (
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)

# Leaves room for the staging root and the two id folders under MAX_PATH.
MAX_NAME_LEN = 120


def sanitize_filename(name: str, fallback: str) -> str:
    """Turn a Telegram file name into one Windows can actually address.

    A trailing space is the case that matters most, because it fails so quietly:
    Windows strips it while resolving a path, so the file gets created under a
    name that no longer matches the one being asked for, and the rename at the
    end of the download comes back as "the parameter is incorrect" -- after the
    whole file has been fetched. One post whose name ended in a space cost about
    nine minutes per attempt and left nothing behind.
    """
    name = os.path.basename(name or "")

    cleaned = "".join(
        "_" if ch in ILLEGAL_CHARS or ord(ch) < 32 else ch for ch in name
    )

    # Trailing dots and spaces are unaddressable, and stripping has to happen
    # after the extension is considered or "file .mp4" would lose nothing while
    # "file. " would lose everything.
    stem, ext = os.path.splitext(cleaned)
    stem = stem.rstrip(". ").lstrip()
    ext = ext.rstrip(". ")

    if stem.upper() in RESERVED_NAMES:
        stem = f"_{stem}"

    if len(stem) > MAX_NAME_LEN:
        stem = stem[:MAX_NAME_LEN].rstrip(". ")

    result = f"{stem}{ext}" if stem else ""
    return result or fallback


def get_download_path(
    folder_id: int,
    filename: str,
    root_dir: Optional[str] = None,
    item_id=None,
) -> str:
    root_dir = root_dir or DOWNLOADS_ROOT
    safe_name = sanitize_filename(filename, fallback=str(item_id or folder_id))
    folder = os.path.join(root_dir, str(folder_id))
    # Telegram hands out the same file_name for every clip recorded in the same
    # second, so two posts in one batch routinely collide. Give each item its own
    # subfolder, or concurrent downloads write into a single file and corrupt it.
    if item_id is not None:
        folder = os.path.join(folder, str(item_id))
    os.makedirs(folder, exist_ok=True)
    full_path = os.path.realpath(os.path.join(folder, safe_name))
    real_root = os.path.realpath(folder)
    if not full_path.startswith(real_root + os.sep) and full_path != real_root:
        safe_name = str(folder_id)
        full_path = os.path.join(folder, safe_name)
    return full_path


def force_remove(path: str) -> bool:
    """Delete a file, including one Windows cannot address by its own name.

    Files created before names were sanitized can end in a space, and Win32
    strips that while resolving a path -- so os.remove reports the file as
    missing while it sits there taking up space and blocking its folder. The
    \\?\\ prefix skips that normalisation. It must be joined to the raw path:
    abspath would strip the trailing space before the prefix could protect it.
    """
    for candidate in (path, "\\\\?\\" + path if os.path.isabs(path) else None):
        if candidate is None:
            continue
        try:
            os.remove(candidate)
            return True
        except FileNotFoundError:
            continue
        except OSError:
            continue
    return False


def cleanup_download(path: str) -> None:
    try:
        LOGGER(__name__).info(f"Cleaning Download: {path}")

        force_remove(path)
        force_remove(path + ".temp")

        # Walk back up the per-item and per-request folders, pruning whatever is
        # left empty, but never past the downloads root itself.
        root = os.path.realpath(DOWNLOADS_ROOT)
        folder = os.path.dirname(os.path.realpath(path))
        while os.path.isdir(folder) and folder != root and folder.startswith(root):
            if os.listdir(folder):
                break
            os.rmdir(folder)
            folder = os.path.dirname(folder)

    except Exception as e:
        LOGGER(__name__).error(f"Cleanup failed for {path}: {e}")


def cleanup_downloads_root(root_dir: Optional[str] = None) -> tuple[int, int]:
    """Clear the staging root, and the pre-DOWNLOAD_DIR one it replaced."""
    if root_dir is None:
        files, freed = _cleanup_tree(DOWNLOADS_ROOT)
        if os.path.realpath(LEGACY_DOWNLOADS_ROOT) != os.path.realpath(DOWNLOADS_ROOT):
            extra_files, extra_freed = _cleanup_tree(LEGACY_DOWNLOADS_ROOT)
            files += extra_files
            freed += extra_freed
        return files, freed

    return _cleanup_tree(root_dir)


def _cleanup_tree(root_dir: str) -> tuple[int, int]:
    if not os.path.isdir(root_dir):
        return 0, 0

    file_count = 0
    total_size = 0

    # Delete bottom-up rather than handing the whole tree to rmtree. rmtree
    # cannot open a file whose name ends in a space, and with ignore_errors it
    # says nothing -- so /cleanup would report success while leaving both the
    # file and every folder above it in place.
    for dirpath, _, filenames in os.walk(root_dir, topdown=False):
        for name in filenames:
            path = os.path.join(dirpath, name)
            try:
                total_size += os.path.getsize(path)
            except OSError:
                pass
            if force_remove(path):
                file_count += 1

        try:
            os.rmdir(dirpath)
        except OSError:
            pass

    shutil.rmtree(root_dir, ignore_errors=True)
    return file_count, total_size


def get_readable_file_size(size_in_bytes: Optional[float]) -> str:
    if size_in_bytes is None or size_in_bytes < 0:
        return "0B"

    for unit in SIZE_UNITS:
        if size_in_bytes < 1024:
            return f"{size_in_bytes:.2f} {unit}"
        size_in_bytes /= 1024

    return "File too large"


def get_readable_time(seconds: int) -> str:
    result = ""
    (days, remainder) = divmod(seconds, 86400)
    days = int(days)
    if days:
        result += f"{days}d"
    (hours, remainder) = divmod(remainder, 3600)
    hours = int(hours)
    if hours:
        result += f"{hours}h"
    (minutes, seconds) = divmod(remainder, 60)
    minutes = int(minutes)
    if minutes:
        result += f"{minutes}m"
    seconds = int(seconds)
    result += f"{seconds}s"
    return result


async def fileSizeLimit(file_size, message, action_type="download", is_premium=False):
    MAX_FILE_SIZE = 2 * 2097152000 if is_premium else 2097152000
    if file_size > MAX_FILE_SIZE:
        await message.reply(
            f"The file size exceeds the {get_readable_file_size(MAX_FILE_SIZE)} limit and cannot be {action_type}ed."
        )
        return False
    return True
