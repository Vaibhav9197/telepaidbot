# Shared concurrency limits for the download -> upload pipeline.
#
# Downloading and uploading used to sit behind a single semaphore held for a
# whole request, so nothing else could start while a video was going out. On a
# home connection the upload is usually the slower leg, which left the download
# side idle for minutes at a time. Gating the two legs separately lets them
# overlap: while one file uploads, the next one downloads.
#
# This does not run into the problem that made parallel connections per file
# slower here. Those competed for the same downstream bandwidth; a download and
# an upload use opposite directions of the link, so overlapping them costs
# neither leg much. Whichever leg is shorter effectively becomes free.
#
# Defaults are one download and one upload at a time, and a depth of 2, so at
# most one file is being fetched while one is being sent.
#
# This lives in its own module because helpers/utils.py needs the same limits as
# main.py and cannot import from main without a circular import.

import os
import asyncio
import shutil
from typing import Optional

from config import PyroConf
from logger import LOGGER

GIB = 1024**3

# How long to wait for in-flight uploads to release disk before refusing.
DISK_WAIT_TIMEOUT = 300
DISK_POLL_INTERVAL = 5

def _depth() -> int:
    # A depth below the download limit would leave download slots permanently
    # unreachable, so never allow that combination.
    return max(PyroConf.PIPELINE_DEPTH, PyroConf.MAX_CONCURRENT_DOWNLOADS)


# Built at import rather than left as None, so reaching one of these before
# init_limits() runs gives correct behaviour instead of an opaque TypeError on
# `async with None`. init_limits() rebuilds them and logs the configuration.
#
# download_semaphore: one download at a time by default. Serialising downloads
# is also what keeps batch order intact -- item N finishes downloading before
# N+1 starts, so N reaches the upload queue first, and Semaphore is FIFO.
download_semaphore = asyncio.Semaphore(PyroConf.MAX_CONCURRENT_DOWNLOADS)

# Gated separately from downloads, and never held at the same time as a download
# slot -- that is precisely what lets the next download start mid-upload.
upload_semaphore = asyncio.Semaphore(PyroConf.MAX_CONCURRENT_UPLOADS)

# Held for a whole item: download, wait, upload, cleanup. Every holder owns a
# file on disk, so this is the disk bound. It applies regardless of how the
# request arrived, which matters because pasting ten links as ten messages would
# otherwise serialise the downloads while leaving ten finished files on disk.
pipeline_semaphore = asyncio.Semaphore(_depth())


def init_limits() -> None:
    global download_semaphore, upload_semaphore, pipeline_semaphore

    download_semaphore = asyncio.Semaphore(PyroConf.MAX_CONCURRENT_DOWNLOADS)
    upload_semaphore = asyncio.Semaphore(PyroConf.MAX_CONCURRENT_UPLOADS)
    pipeline_semaphore = asyncio.Semaphore(_depth())

    LOGGER(__name__).info(
        f"Pipeline limits: {PyroConf.MAX_CONCURRENT_DOWNLOADS} download(s) + "
        f"{PyroConf.MAX_CONCURRENT_UPLOADS} upload(s) concurrently, "
        f"depth {_depth()}, {PyroConf.DISK_MIN_FREE_GB} GB held in reserve"
    )


def downloads_volume(path: Optional[str] = None) -> str:
    """A real path on the volume that will hold downloads.

    The download root is created on demand by get_download_path and removed again
    by /cleanup, so it may legitimately not exist when we need to measure it.
    disk_usage needs a path that exists; the nearest existing parent is on the
    same volume, which is all we are actually asking about.
    """
    path = path or PyroConf.DOWNLOAD_DIR

    while not os.path.exists(path):
        parent = os.path.dirname(path)
        if not parent or parent == path:
            return "."
        path = parent

    return path


def free_space(path: Optional[str] = None) -> int:
    """Free bytes on the volume that will actually hold the download.

    Checking the working directory was right only while downloads landed inside
    the checkout. They now stage under PyroConf.DOWNLOAD_DIR, which is routinely
    a different volume, and measuring the wrong drive either blocks a download
    that would have fit or admits one that cannot.
    """
    return shutil.disk_usage(downloads_volume(path)).free


async def ensure_disk_space(needed_bytes: int, message=None) -> bool:
    """Wait until the file plus the reserve will fit on disk.

    Starting a 600 MB download onto a nearly full disk fails most of the way
    through and takes the sequential retry down with it. An in-flight upload is
    usually about to release its own file, so poll briefly before refusing
    rather than beginning a transfer that cannot finish.
    """
    reserve = PyroConf.DISK_MIN_FREE_GB * GIB
    required = max(0, needed_bytes) + reserve

    waited = 0
    warned = False

    while free_space() < required:
        if waited >= DISK_WAIT_TIMEOUT:
            shortfall = (required - free_space()) / GIB
            LOGGER(__name__).error(
                f"Gave up waiting for disk space after {waited}s; "
                f"{shortfall:.2f} GB short"
            )
            if message is not None:
                await message.reply(
                    "**❌ Not enough disk space.**\n"
                    f"Need about `{shortfall:.2f} GB` more "
                    f"(keeping `{PyroConf.DISK_MIN_FREE_GB} GB` in reserve).\n"
                    "Free some space or run /cleanup, then try again."
                )
            return False

        if not warned:
            LOGGER(__name__).warning(
                f"Waiting for disk space: need "
                f"{required / GIB:.2f} GB, have {free_space() / GIB:.2f} GB"
            )
            warned = True

        await asyncio.sleep(DISK_POLL_INTERVAL)
        waited += DISK_POLL_INTERVAL

    return True
