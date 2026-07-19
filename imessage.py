#!/usr/bin/env python3
"""
Small helper for sending iMessages via AppleScript and normalizing iMessage handles.
"""
import logging
import re
import subprocess
import time

LOG = logging.getLogger("devin_bridge")


def normalize_handle(value):
    """Lowercase, strip spaces, drop E: email prefix.
    For phone numbers: keep digits only.
    For emails: preserve the full address (don't strip non-digits)."""
    v = value.strip().lower()
    if v.startswith("e:"):
        v = v[2:]
    if "@" in v:
        return v
    digits = re.sub(r"\D", "", v)
    return digits if digits else v


_SCRIPT = """
on run argv
    set msg to item 1 of argv
    set targetHandle to item 2 of argv
    tell application "Messages"
        set targetService to 1st service whose service type = iMessage
        set targetBuddy to buddy targetHandle of targetService
        send msg to targetBuddy
    end tell
end run
"""


def send_imessage(handle, text, chunk_size=1500):
    """Send text as an iMessage to `handle`. Long messages are split into chunks.
    Retries up to 3 times with a 30s timeout each."""
    if not handle or not text:
        return False
    chunks = [text[i:i + chunk_size] for i in range(0, len(text), chunk_size)]
    for chunk in chunks:
        last_err = "unknown"
        for attempt in range(3):
            try:
                proc = subprocess.run(
                    ["osascript", "-e", _SCRIPT, chunk, handle],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                stdout = (proc.stdout or "").strip()
                stderr = (proc.stderr or "").strip()
                if proc.returncode == 0:
                    if stdout:
                        LOG.debug("osascript stdout: %s", stdout)
                    break
                last_err = stderr or stdout or f"exit {proc.returncode}"
                LOG.warning("osascript attempt %d failed: %s", attempt + 1, last_err)
            except subprocess.TimeoutExpired:
                last_err = "30s timeout"
                LOG.warning("osascript attempt %d timed out", attempt + 1)
                time.sleep(2)
        else:
            raise RuntimeError(f"osascript failed after 3 attempts: {last_err}")
    return True
