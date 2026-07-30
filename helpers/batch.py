# Persistent record of the last batch, so an interrupted run can be resumed.
#
# The batch loops kept their progress in local variables, so a dropped link, a
# crash or a /killall took the whole picture with it. The user was left knowing
# that "12 of 60" had arrived but not which 48 were missing, and the only way
# back was to re-run the entire range and re-download everything that had already
# succeeded -- which on a slow line costs more than the failures did.
#
# Outcomes are recorded per item rather than counted, because "it failed" and
# "it will always fail" are different things. A post that exceeds the size limit,
# or a poll, fails identically on every future attempt; a post lost to a dropped
# connection does not. Retrying the first kind is pure waste and makes /retry
# look broken, so the two are kept apart from the start.

import os
import json
from datetime import datetime
from typing import List, Optional

from logger import LOGGER

# Item outcomes.
OK = "ok"        # delivered
RETRY = "retry"  # failed in a way a later attempt could plausibly fix
SKIP = "skip"    # nothing to deliver, or a failure no retry can fix

# Kept next to the code rather than under DOWNLOAD_DIR: the staging directory
# defaults into %TEMP%, which Windows is free to clear, and a resume point that
# evaporates on reboot is not much of a resume point. *.json is gitignored.
STATE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "batch_state.json"
)


class BatchState:
    """The remaining work in one batch, durable across restarts."""

    def __init__(
        self,
        kind: str,
        prefix: str,
        ids: List[int],
        pending: Optional[List[int]] = None,
        done: Optional[List[int]] = None,
        skipped: Optional[List[int]] = None,
        failed: Optional[List[int]] = None,
        started: Optional[str] = None,
    ):
        self.kind = kind                      # "posts" or "stories"
        self.prefix = prefix                  # everything before the trailing id
        self.ids = list(ids)
        self.pending = list(ids) if pending is None else list(pending)
        self.done = list(done or [])
        self.skipped = list(skipped or [])
        self.failed = list(failed or [])
        self.started = started or datetime.now().isoformat(timespec="seconds")

    # -- shape -------------------------------------------------------------

    def url_for(self, item_id: int) -> str:
        return f"{self.prefix}/{item_id}"

    def remaining(self) -> List[int]:
        """Items worth attempting: never tried, plus those that failed.

        Skipped items are deliberately absent. They are the ones a retry cannot
        help, and including them would mean every /retry re-reports the same
        polls and oversized files forever.
        """
        return sorted(set(self.pending) | set(self.failed))

    def is_complete(self) -> bool:
        return not self.remaining()

    def counts(self) -> dict:
        return {
            "done": len(self.done),
            "failed": len(self.failed),
            "skipped": len(self.skipped),
            "pending": len(self.pending),
            "total": len(self.ids),
        }

    # -- transitions -------------------------------------------------------

    def mark(self, item_id: int, outcome: str) -> None:
        for bucket in (self.pending, self.done, self.skipped, self.failed):
            if item_id in bucket:
                bucket.remove(item_id)

        if outcome == OK:
            self.done.append(item_id)
        elif outcome == SKIP:
            self.skipped.append(item_id)
        else:
            self.failed.append(item_id)

    def restore(self, item_id: int) -> None:
        """Put an item back as untried, for work cancelled mid-flight.

        A cancelled item was never judged, so recording it as failed would be a
        guess. Pending says exactly what is true: it still needs doing.
        """
        for bucket in (self.done, self.skipped, self.failed):
            if item_id in bucket:
                bucket.remove(item_id)
        if item_id not in self.pending:
            self.pending.append(item_id)

    # -- persistence -------------------------------------------------------

    def save(self) -> None:
        payload = {
            "kind": self.kind,
            "prefix": self.prefix,
            "ids": self.ids,
            "pending": sorted(self.pending),
            "done": sorted(self.done),
            "skipped": sorted(self.skipped),
            "failed": sorted(self.failed),
            "started": self.started,
        }
        try:
            # Write beside the target and swap it in, so a crash midway through
            # leaves the previous resume point intact rather than a half file.
            tmp = STATE_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2)
            os.replace(tmp, STATE_PATH)
        except OSError as e:
            LOGGER(__name__).warning(f"Could not save batch state: {e}")

    @classmethod
    def load(cls) -> Optional["BatchState"]:
        if not os.path.exists(STATE_PATH):
            return None
        try:
            with open(STATE_PATH, encoding="utf-8") as fh:
                data = json.load(fh)
            return cls(
                kind=data["kind"],
                prefix=data["prefix"],
                ids=data["ids"],
                pending=data.get("pending"),
                done=data.get("done"),
                skipped=data.get("skipped"),
                failed=data.get("failed"),
                started=data.get("started"),
            )
        except (OSError, ValueError, KeyError) as e:
            LOGGER(__name__).warning(f"Could not read batch state: {e}")
            return None

    @staticmethod
    def clear() -> None:
        for path in (STATE_PATH, STATE_PATH + ".tmp"):
            try:
                if os.path.exists(path):
                    os.remove(path)
            except OSError as e:
                LOGGER(__name__).warning(f"Could not clear batch state: {e}")
