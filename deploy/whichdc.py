# Prints which Telegram datacenter your SESSION_STRING lives on, so you can put
# the VPS next to it. Transfer speed is dominated by the round trip to that DC:
# every one of DOWNLOAD_WORKERS parallel connections pays the latency on every
# chunk, so a box 5 ms away and one 180 ms away are not the same machine.
#
# Reads config.env locally and connects to nothing. It never prints the session
# string or the auth key.

import base64
import binascii
import struct
import sys
from os import getenv
from pathlib import Path

from dotenv import load_dotenv

# Where Telegram actually keeps each DC, and the nearest cloud region to it.
DCS = {
    1: ("Miami, USA", "us-ashburn-1 / us-phoenix-1"),
    2: ("Amsterdam, Netherlands", "eu-amsterdam-1 / eu-frankfurt-1"),
    3: ("Miami, USA", "us-ashburn-1 / us-phoenix-1"),
    4: ("Amsterdam, Netherlands", "eu-amsterdam-1 / eu-frankfurt-1"),
    5: ("Singapore", "ap-singapore-1"),
}

# Pyrogram/kurigram pack the session as base64 over a fixed struct. The layout
# changed between major versions and only the leading dc_id byte is stable, so
# match on decoded length rather than assuming one format.
LAYOUTS = {
    # dc_id, api_id, test_mode, auth_key, user_id, is_bot
    struct.calcsize(">BI?256sQ?"): ">BI?256sQ?",
    # older: dc_id, test_mode, auth_key, user_id, is_bot
    struct.calcsize(">B?256sI?"): ">B?256sI?",
    struct.calcsize(">B?256sQ?"): ">B?256sQ?",
}


def decode_dc(session_string):
    # The stored string drops base64 padding; put it back before decoding.
    padded = session_string + "=" * (-len(session_string) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"SESSION_STRING is not valid base64: {exc}")

    layout = LAYOUTS.get(len(raw))
    if layout is None:
        # Unknown version, but byte 0 is dc_id in every layout shipped so far.
        if not raw:
            raise ValueError("SESSION_STRING decoded to nothing")
        return raw[0], None
    return struct.unpack(layout, raw)[0], layout


def main():
    root = Path(__file__).resolve().parent.parent
    load_dotenv(root / "config.env.local")
    load_dotenv(root / "config.env")

    session_string = (getenv("SESSION_STRING") or "").strip()
    if not session_string:
        print("No SESSION_STRING found in config.env or config.env.local.")
        return 1

    try:
        dc_id, layout = decode_dc(session_string)
    except ValueError as exc:
        print(f"Could not read the session: {exc}")
        return 1

    location, region = DCS.get(dc_id, ("unknown", "unknown"))
    print(f"Session home DC : DC{dc_id} ({location})")
    print(f"Deploy the VPS in: {region}")
    if layout is None:
        print("Note: unrecognised session layout; dc_id read from the first byte.")
    if dc_id not in DCS:
        print("Note: unknown DC id. Telegram may have added a datacenter.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
