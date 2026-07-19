#!/usr/bin/env python3
"""
iMessage bridge for Devin Cloud.
Reads incoming iMessages from ~/Library/Messages/chat.db and dispatches them to Devin.
Replies with session URLs, PR links, status/estimates, and can trigger Heroku migrations.
"""
import json
import logging
import os
import re
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import builder
import devin_usage
import imessage

DB_PATH = Path.home() / "Library/Messages/chat.db"
STATE_FILE = Path(__file__).with_suffix(".state.json")
LOG_DIR = Path(__file__).parent / "logs"
REPO_ROOT = Path(__file__).resolve().parent
MIGRATE_SCRIPT = REPO_ROOT / "migrate.py"
CONTEXT_FILE = Path(__file__).with_name("CONTEXT.md")

# Claude Pro CLI — used to handle general (non-coding) messages and to distill
# verbose coding prompts into tight specs before sending to Devin.
# These paths point to the node binary and cli.js bundled with Claude Code.
_CLAUDE_NODE = Path("/Users/gzemserver/.devin-server/bin/0d4bf12ed4a7597cb8ae9016fe8474468aad98a2/node")
_CLAUDE_CLI = Path("/Users/gzemserver/.devin-server/extensions/anthropic.claude-code-2.1.89-universal/resources/claude-code/cli.js")

POLL_INTERVAL = float(os.environ.get("POLL_SECONDS", "5"))
SESSION_TIMEOUT = 7200
# Apple's CoreData timestamp epoch offset (seconds from Unix epoch to 2001-01-01 UTC)
APPLE_EPOCH_OFFSET = 978307200
# Messages older than this are skipped — catches stale iMessage-synced reminders/notes
# that arrive in chat.db with fresh ROWIDs but old creation times.
MAX_MESSAGE_AGE = float(os.environ.get("MAX_MESSAGE_AGE_SECONDS", "3600"))

LOG = logging.getLogger("devin_bridge")
STATE_LOCK = threading.Lock()

# After send_reply() completes, we record current_max_rowid() here so the main
# loop can use max(state_rowid, _post_send_rowid) as its effective watermark.
# This prevents the bridge from re-processing its own outgoing messages when
# is_from_me=1 filtering is relaxed (needed for same-Apple-ID senders).
_post_send_rowid: int = 0
_post_send_lock = threading.Lock()

# Persists the max rowid after each send so restarts don't replay our own replies.
_SENT_WATERMARK_FILE = Path(__file__).with_suffix(".sent_watermark")


def _write_sent_watermark(rowid: int):
    try:
        _SENT_WATERMARK_FILE.write_text(str(rowid))
    except Exception:
        pass


def _read_sent_watermark() -> int:
    try:
        return int(_SENT_WATERMARK_FILE.read_text().strip())
    except Exception:
        return 0

# Text prefixes that identify bridge-generated outgoing messages.
# Used as a secondary loop guard: skip any incoming message whose body starts
# with one of these, since we could never want to relay our own replies to Devin.
_BRIDGE_PREFIXES = (
    "Devin Instance:",
    "Session cleared.",
    "Commands:\n",
    "Running now.",
    "Migration started",
    "No active session.",
    "Session stopped:",
    "Session timed out",
    "Pulled.",
    "Approval cancelled.",
    "Devin API error:",
    "Failed to start session",
    "PR: https://",
    "Done.",
)

COMMANDS_HELP = (
    "Commands:\n"
    "• status — ACU usage & quota\n"
    "• estimate <task> — cost estimate\n"
    "• session <id> — look up a session\n"
    "• migrate <cmd> — DB migration\n"
    "• create/build/scaffold <desc> — new project\n"
    "• new / reset — start a fresh Devin session\n"
    "• url — get the current session URL\n"
    "• update — git pull + restart bridge with new code"
)


def preflight(config):
    """Exit early with a clear message if the environment is misconfigured."""
    problems = []
    if not config.get("api_key"):
        problems.append("DEVIN_API_KEY is not set.")
    if not config.get("allowed_senders"):
        problems.append("ALLOWED_SENDERS is not set (needed so only you can trigger Devin).")
    if not DB_PATH.exists():
        problems.append(f"Cannot find {DB_PATH}. Is this a Mac with Messages set up?")
    if problems:
        for p in problems:
            LOG.error("PREFLIGHT: %s", p)
        raise SystemExit(1)


def _load_context():
    """Load default prompt context from CONTEXT.md or DEVIN_CONTEXT env."""
    env_ctx = os.environ.get("DEVIN_CONTEXT", "")
    if env_ctx:
        return env_ctx
    if CONTEXT_FILE.exists():
        return CONTEXT_FILE.read_text()
    return ""


def get_config():
    raw_senders = [s.strip() for s in os.environ.get("ALLOWED_SENDERS", "").split(",") if s.strip()]
    if not raw_senders:
        raise SystemExit("ALLOWED_SENDERS is required (comma-separated iMessage handles)")
    api_key = os.environ.get("DEVIN_API_KEY")
    if not api_key:
        raise SystemExit("DEVIN_API_KEY is required")
    return {
        "api_key": api_key,
        "allowed_senders": raw_senders,
        "allowed_normalized": {imessage.normalize_handle(s) for s in raw_senders},
        "org_id": os.environ.get("DEVIN_ORG_ID", ""),
        "context": _load_context(),
        "usage_guard": os.environ.get("USAGE_GUARD", "1").strip().lower() not in ("0", "false", "no", "off"),
        "quota": float(os.environ["DEVIN_MONTHLY_ACU_QUOTA"]) if os.environ.get("DEVIN_MONTHLY_ACU_QUOTA") else None,
        "start_fresh": os.environ.get("START_FRESH", "1").strip().lower() not in ("0", "false", "no", "off"),
        "reply_to": os.environ.get("REPLY_TO", "").strip() or None,
    }


def save_state(state):
    try:
        with STATE_LOCK:
            with open(STATE_FILE, "w") as f:
                json.dump(state, f)
    except Exception as e:
        LOG.error("Failed to save state: %s", e)


def load_state():
    try:
        if STATE_FILE.exists():
            with open(STATE_FILE) as f:
                return json.load(f)
    except Exception as e:
        LOG.error("Failed to load state: %s", e)
    return {"last_rowid": 0, "pending": {}, "active": {}}


def _get_active_session(state, sender):
    return state.get("active", {}).get(imessage.normalize_handle(sender))


def _set_active_session(state, sender, session_id, url):
    state.setdefault("active", {})[imessage.normalize_handle(sender)] = {
        "session_id": session_id,
        "url": url,
    }
    save_state(state)


def _clear_active_session(state, sender):
    state.setdefault("active", {}).pop(imessage.normalize_handle(sender), None)
    save_state(state)


def decode_attributed_body(blob: bytes) -> str:
    """Best-effort extraction of plain text from an attributedBody blob.
    Newer macOS versions leave the plain `text` column NULL and put the content here."""
    if not blob:
        return ""
    try:
        text = blob.decode("utf-8", errors="ignore")
        # Match the NSString value after the length/type varint bytes.
        # The NSString tag is followed by a 1-3 byte varint then the string.
        # We look for a run of printable ASCII/Unicode that starts with a
        # letter/digit/punctuation (not a stray control or length byte).
        m = re.search(r"NSString[\x00-\x1f\x80-\xff]{1,6}([A-Za-z0-9\"'({\[!@#$%^&*_\-+=|~`<>?/.,;: ][^\x00-\x08\x0b\x0c\x0e-\x1f]{1,})", text)
        if m:
            return m.group(1).strip("\x00 ").strip()
    except Exception:
        pass
    return ""


def _db_connect():
    """Open chat.db read-only so SQLite handles the WAL transparently."""
    uri = f"file:{DB_PATH}?mode=ro"
    return sqlite3.connect(uri, uri=True, timeout=5)


def current_max_rowid() -> int:
    """Return the highest ROWID currently in the message table."""
    try:
        conn = _db_connect()
        row = conn.execute("SELECT COALESCE(MAX(ROWID), 0) FROM message").fetchone()
        conn.close()
        return int(row[0]) if row else 0
    except Exception as e:
        LOG.warning("current_max_rowid failed: %s", e)
        return 0


def get_new_messages(last_rowid, allowed_normalized):
    # Two-part query:
    # 1. is_from_me=0 → normal incoming messages from other people/phones.
    # 2. is_from_me=1 from email addresses → messages sent from another Apple
    #    device sharing the same Apple ID (e.g. iPhone → Mac mini via
    #    grahamzemel126@gmail.com). Only email handles can appear this way;
    #    phone numbers always arrive as is_from_me=0.
    #    We keep is_from_me=0 as the default filter because reading ALL rows
    #    (no filter) can trigger macOS authorization errors on protected messages.
    email_handles = [h for h in allowed_normalized if "@" in h]
    if email_handles:
        placeholders = ",".join("?" * len(email_handles))
        query = f"""
            SELECT m.ROWID, m.text, m.attributedBody, m.date, h.id
            FROM message m
            JOIN handle h ON m.handle_id = h.ROWID
            WHERE m.ROWID > ?
              AND (m.is_from_me = 0 OR h.id IN ({placeholders}))
            ORDER BY m.ROWID ASC
        """
        params = [last_rowid] + email_handles
    else:
        query = """
            SELECT m.ROWID, m.text, m.attributedBody, m.date, h.id
            FROM message m
            JOIN handle h ON m.handle_id = h.ROWID
            WHERE m.is_from_me = 0
              AND m.ROWID > ?
            ORDER BY m.ROWID ASC
        """
        params = [last_rowid]
    try:
        conn = _db_connect()
        cur = conn.cursor()
        cur.execute(query, params)
        rows = cur.fetchall()
        conn.close()
    except Exception as e:
        LOG.error("DB query failed: %s", e)
        return []

    now = time.time()
    messages = []
    for rowid, text, attributed_body, apple_date, handle in rows:
        if not handle:
            continue
        # Skip messages whose creation time is older than MAX_MESSAGE_AGE.
        # This filters stale iMessage-synced notes/reminders that arrive with
        # fresh ROWIDs but old Apple timestamps.
        # macOS < 10.15 stores seconds; 10.15+ stores nanoseconds. Detect by magnitude.
        if apple_date:
            divisor = 1e9 if apple_date > 1e12 else 1.0
            msg_unix = apple_date / divisor + APPLE_EPOCH_OFFSET
            age = now - msg_unix
            if age > MAX_MESSAGE_AGE:
                LOG.debug("Skipping stale message (age %.0fs, rowid %d)", age, rowid)
                continue
        norm = imessage.normalize_handle(handle)
        if norm in allowed_normalized:
            body = (text or "").strip()
            if not body and attributed_body:
                body = decode_attributed_body(attributed_body)
            if not body:
                continue
            # Skip bridge's own outgoing messages (secondary loop guard).
            # Strip leading decode artifacts (e.g. "+N" from attributedBody)
            # before prefix-matching so garbled headers don't bypass the check.
            body_stripped = re.sub(r"^[^A-Za-z0-9\"'({\[!@#$%^&]+", "", body)
            if body_stripped.startswith(_BRIDGE_PREFIXES):
                LOG.debug("Skipping bridge-generated message (rowid %d)", rowid)
                continue
            messages.append({"rowid": rowid, "text": body, "handle": handle})
    return messages


def send_reply(handle, text):
    global _post_send_rowid
    LOG.info("Sending reply to %s: %s", handle, text[:120])
    try:
        imessage.send_imessage(handle, text)
        # Record the max rowid after our send so the main loop skips our own
        # outgoing message (which lands in chat.db as is_from_me=1). Written
        # to disk so restarts don't replay the bridge's own replies.
        new_max = current_max_rowid()
        with _post_send_lock:
            if new_max > _post_send_rowid:
                _post_send_rowid = new_max
        _write_sent_watermark(new_max)
    except Exception as e:
        LOG.error("Failed to send reply: %s", e)


def call_devin_api(config, prompt):
    import urllib.request
    import urllib.error

    full_prompt = (config["context"] + "\n\n" + prompt) if config["context"] else prompt
    headers = {
        "Authorization": f"Bearer {config['api_key']}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if config["org_id"]:
        url = f"https://api.devin.ai/v3/organizations/{config['org_id']}/sessions"
        payload = {"prompt": full_prompt}
    else:
        url = "https://api.devin.ai/v1/sessions"
        payload = {"prompt": full_prompt, "idempotent": True}

    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return {"ok": True, "session_id": data.get("session_id", ""), "url": data.get("url", ""), "data": data}
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        return {"ok": False, "error": f"HTTP {e.code}: {body[:300]}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def send_message_to_session(config, session_id, message):
    import urllib.request
    import urllib.error

    headers = {
        "Authorization": f"Bearer {config['api_key']}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    devin_id = session_id if session_id.startswith("devin-") else f"devin-{session_id}"
    if config["org_id"]:
        url = f"https://api.devin.ai/v3/organizations/{config['org_id']}/sessions/{devin_id}/messages"
        payload = {"message": message}
    else:
        url = f"https://api.devin.ai/v1/sessions/{session_id}/message"
        payload = {"message": message}

    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return {"ok": True}
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        return {"ok": False, "error": f"HTTP {e.code}: {body[:300]}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def poll_devin_session(config, session_id):
    import urllib.request
    import urllib.error

    devin_id = session_id if session_id.startswith("devin-") else f"devin-{session_id}"
    headers = {"Authorization": f"Bearer {config['api_key']}", "Accept": "application/json"}
    if config["org_id"]:
        url = f"https://api.devin.ai/v3/organizations/{config['org_id']}/sessions/{devin_id}"
    else:
        url = f"https://api.devin.ai/v1/sessions/{session_id}"

    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        return {"status": "error", "status_detail": f"HTTP {e.code}: {body[:300]}"}
    except Exception as e:
        return {"status": "error", "status_detail": str(e)}


def fetch_devin_messages(config, session_id):
    """Fetch the messages list from the v3 /messages endpoint.
    Returns a list of {source, message} dicts, or [] on error."""
    import urllib.request
    import urllib.error

    if not config["org_id"]:
        return []
    devin_id = session_id if session_id.startswith("devin-") else f"devin-{session_id}"
    url = f"https://api.devin.ai/v3/organizations/{config['org_id']}/sessions/{devin_id}/messages"
    headers = {"Authorization": f"Bearer {config['api_key']}", "Accept": "application/json"}
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read()).get("items", [])
    except Exception as e:
        LOG.debug("fetch_devin_messages failed: %s", e)
        return []


def extract_devin_reply(data, messages=None):
    """Extract the final text reply from a finished Devin session.
    `messages` is an optional list from the v3 /messages endpoint."""
    so = data.get("structured_output") or {}
    if isinstance(so, dict):
        for key in ("reply", "summary", "output", "response", "result", "pull_request_url", "pr_url"):
            if so.get(key):
                return str(so[key])
    elif isinstance(so, str) and so.strip():
        return so.strip()
    # v3 API: messages come from the separate /messages endpoint as {source, message}
    if messages:
        for item in reversed(messages):
            if item.get("source") == "devin" and item.get("message"):
                return str(item["message"])
    # v1 API: messages embedded in session data
    msgs = data.get("messages") or []
    for msg in reversed(msgs):
        if (
            msg.get("role") in ("assistant", "devin", "agent")
            or msg.get("type") in ("devin_message", "devin")
        ):
            content = msg.get("content") or msg.get("text") or msg.get("message") or ""
            if content:
                return str(content)
    return ""


def extract_pr_url(data):
    prs = data.get("pull_requests") or []
    if prs and prs[0].get("pr_url"):
        return prs[0]["pr_url"]
    pr = data.get("pull_request") or {}
    return pr.get("pr_url") or ""


def _poll_session_until_done(config, session_id, sender, state):
    """Poll a Devin session until it finishes or errors, then clear it."""
    _DONE_ENUM = {"finished", "expired", "stopped"}
    _TERMINAL_STATUS = {"error", "suspended", "exit"}
    _TERMINAL_DETAIL = {"error", "out_of_credits", "out_of_quota", "usage_limit_exceeded", "no_quota_allocation"}

    deadline = time.time() + SESSION_TIMEOUT
    while time.time() < deadline:
        data = poll_devin_session(config, session_id)
        status = data.get("status", "")
        detail = data.get("status_detail", "")
        status_enum = data.get("status_enum", "")
        pr_url = extract_pr_url(data)

        # "blocked" / "waiting_for_user" = Devin sent a message and is waiting
        # for more input. Forward only NEW Devin messages (not ones already sent)
        # by comparing event_ids. v1 uses status_enum="blocked";
        # v3 uses status_detail="waiting_for_user".
        is_waiting = status_enum == "blocked" or detail == "waiting_for_user"
        if is_waiting:
            items = fetch_devin_messages(config, session_id)
            devin_items = [i for i in items if i.get("source") == "devin" and i.get("message")]
            if devin_items:
                last = devin_items[-1]
                active = _get_active_session(state, sender)
                seen_event_id = (active or {}).get("last_devin_event_id")
                if last.get("event_id") != seen_event_id:
                    # New message from Devin — forward it and record we've sent it.
                    if active:
                        active["last_devin_event_id"] = last.get("event_id")
                        save_state(state)
                    send_reply(sender, last["message"])
                    # Check if Devin pushed new commits while working on this task.
                    threading.Thread(
                        target=_check_and_auto_update, args=(sender,), daemon=True
                    ).start()
                    # Exit poll; session stays alive for the next user message.
                    return
                # Same event_id: Devin hasn't responded to the latest user
                # message yet (still processing). Keep polling.
            time.sleep(10)
            continue

        is_done = detail == "finished" or status == "exit" or status_enum in _DONE_ENUM
        is_error = (
            status in _TERMINAL_STATUS and not is_done
            or detail in _TERMINAL_DETAIL
        )

        if is_done:
            # Guard against two concurrent poll threads both sending a reply.
            current = _get_active_session(state, sender)
            if current and current.get("session_id") == session_id:
                _clear_active_session(state, sender)
                devin_usage.invalidate_credits_cache()
                items = fetch_devin_messages(config, session_id)
                devin_items = [i for i in items if i.get("source") == "devin" and i.get("message")]
                seen_event_id = current.get("last_devin_event_id")
                # Collect all unsent Devin messages
                if seen_event_id:
                    new_items = []
                    past = False
                    for i in items:
                        if i.get("event_id") == seen_event_id:
                            past = True
                            continue
                        if past and i.get("source") == "devin" and i.get("message"):
                            new_items.append(i)
                    reply = "\n\n".join(i["message"] for i in new_items) if new_items else ""
                else:
                    reply = extract_devin_reply(data, items)
                parts = []
                if reply:
                    parts.append(reply)
                if pr_url:
                    parts.append(f"PR: {pr_url}")
                send_reply(sender, "\n\n".join(parts) if parts else "Done.")
                # Check if Devin pushed new commits; auto-pull and restart if so.
                threading.Thread(
                    target=_check_and_auto_update, args=(sender,), daemon=True
                ).start()
            return
        if is_error:
            _clear_active_session(state, sender)
            send_reply(sender, f"Session stopped: {detail or status or status_enum or 'unknown'}")
            return
        time.sleep(10)

    _clear_active_session(state, sender)
    send_reply(sender, f"Session timed out after {SESSION_TIMEOUT // 3600}h. Check with `session {session_id}`.")


def _start_poll(config, session_id, sender, state, blocking):
    if blocking:
        _poll_session_until_done(config, session_id, sender, state)
    else:
        threading.Thread(
            target=lambda: _poll_session_until_done(config, session_id, sender, state),
            daemon=True,
        ).start()


def run_session(config, prompt, sender, state, *, blocking=False):
    result = call_devin_api(config, prompt)
    if not result["ok"]:
        send_reply(sender, f"Devin API error: {result['error']}")
        return

    session_id = result.get("session_id") or ""
    url = result.get("url") or ""

    if not session_id:
        send_reply(sender, "Failed to start session (no session ID returned).")
        return

    _set_active_session(state, sender, session_id, url)
    if url:
        send_reply(sender, f"Devin Instance: {url}\n\n{COMMANDS_HELP}")
    _start_poll(config, session_id, sender, state, blocking)


def _check_and_auto_update(sender):
    """After any Devin reply, fetch origin/main and auto-pull+restart if new
    commits landed (direct push or merged PR). Runs in a daemon thread."""
    try:
        subprocess.run(
            ["git", "-C", str(REPO_ROOT), "fetch", "origin", "main", "--quiet"],
            capture_output=True, timeout=30,
        )
        local = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
        remote = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "rev-parse", "origin/main"],
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
        if not remote or local == remote:
            return  # Nothing new on main
        result = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "pull", "--ff-only", "origin", "main"],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode == 0:
            out = (result.stdout + result.stderr).strip()
            send_reply(sender, f"Bridge updated from GitHub. Restarting...\n{out[:150]}")
            time.sleep(1)
            os.kill(os.getpid(), signal.SIGTERM)
        else:
            LOG.warning("Auto-update pull failed: %s", (result.stdout + result.stderr).strip())
    except Exception as e:
        LOG.warning("Auto-update check failed: %s", e)


def _do_update(sender):
    """Pull latest code from git, notify the user, then SIGTERM ourselves.
    KeepAlive=true in the plist means launchd immediately restarts the bridge
    with the freshly pulled code — no manual launchctl commands needed."""
    try:
        result = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "pull", "--ff-only"],
            capture_output=True, text=True, timeout=60,
        )
        output = (result.stdout + result.stderr).strip() or "Already up to date."
        if result.returncode != 0:
            send_reply(sender, f"git pull failed: {output[:300]}")
            return
        send_reply(sender, f"Pulled. Restarting bridge with new code...\n{output[:200]}")
    except Exception as e:
        send_reply(sender, f"Update failed: {e}")
        return
    # send_reply is synchronous (osascript blocks until sent), so the message
    # is delivered before we kill ourselves.
    time.sleep(1)
    os.kill(os.getpid(), signal.SIGTERM)


def _call_claude(user_text, system_prompt, timeout=90):
    """Run claude CLI non-interactively using the bundled node + cli.js.
    `user_text` is piped via stdin; `system_prompt` is passed as -p argument.
    Returns the stripped output string, or None on failure."""
    if not _CLAUDE_NODE.exists() or not _CLAUDE_CLI.exists():
        LOG.warning("Claude CLI not found at expected path — skipping")
        return None
    try:
        result = subprocess.run(
            [str(_CLAUDE_NODE), str(_CLAUDE_CLI), "-p", system_prompt],
            input=user_text,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode == 0:
            return result.stdout.strip()
        LOG.warning("claude CLI exited %d: %s", result.returncode, result.stderr[:200])
        return None
    except subprocess.TimeoutExpired:
        LOG.warning("claude CLI timed out after %ds", timeout)
        return None
    except Exception as e:
        LOG.warning("claude CLI error: %s", e)
        return None


_CLASSIFY_PROMPT = (
    "Classify the following message. Does it require actual code to be written, "
    "debugged, deployed, or does it mention a specific repo, PR, or technical "
    "implementation task? Reply with exactly one word: coding or general. "
    "If uncertain, reply general."
)

_ANSWER_PROMPT = (
    "Answer the user's message concisely and helpfully in plain text. "
    "CRITICAL: No markdown whatsoever — no asterisks, no backticks, no hyphens as bullets, "
    "no pound signs, no bold, no italic. The reply is sent as an iMessage. "
    "Use plain sentences. For lists, use 1. 2. 3. numbering or just commas."
)

_DISTILL_PROMPT = (
    "You are routing a task to Devin, an autonomous coding AI. "
    "Distill the following message into a tight, precise technical specification. "
    "Keep it to 3-5 sentences. Focus on what must be built or changed, the desired outcome, "
    "and any key constraints. Plain text only — no markdown, no bullet points."
)


def route_message(config, text, sender, state, *, blocking=False):
    """Classify the message and route to Claude (general) or Devin (coding).

    Routing rules:
    1. General question / idea / planning → Claude answers directly (free, no ACUs).
    2. Coding task + Devin available → Claude distills the prompt → Devin executes.
    3. Coding task + Devin out of credits → Claude handles it, user is notified.
    4. If classify step fails → fall back to Devin (or Claude if Devin unavailable).
    """
    # Step 1: classify with Claude.
    classification = _call_claude(text, _CLASSIFY_PROMPT, timeout=30)
    LOG.info("Claude classified message as: %s", (classification or "FAILED")[:20])

    is_coding = classification is not None and "coding" in classification.lower()

    if not is_coding:
        # General question / idea / planning — Claude handles it directly.
        answer = _call_claude(text, _ANSWER_PROMPT, timeout=60)
        if answer:
            send_reply(sender, answer)
        else:
            # Claude failed — last resort: send to Devin as a general prompt.
            full_prompt = (config["context"] + "\n\n" + text) if config["context"] else text
            run_session(config, full_prompt, sender, state, blocking=blocking)
        return

    # Step 2: coding task — check if Devin has capacity.
    credits_ok, credits_reason = devin_usage.devin_credits_ok()
    if not credits_ok:
        LOG.info("Devin out of credits (%s) — routing coding task to Claude", credits_reason)
        answer = _call_claude(
            text,
            (
                _ANSWER_PROMPT + " Note: the user intended this as a coding task, so do your best "
                "to help with architecture, code snippets, or a plan even though the coding AI "
                "(Devin) is currently unavailable."
            ),
            timeout=90,
        )
        notice = f"Devin is unavailable: {credits_reason} Claude is handling this instead."
        if answer:
            send_reply(sender, notice + "\n\n" + answer)
        else:
            send_reply(sender, notice + " Claude also failed to respond. Try again later.")
        return

    # Step 3: Devin is available — distill the prompt for efficiency, then hand off.
    distilled = _call_claude(text, _DISTILL_PROMPT, timeout=60)
    if distilled:
        LOG.info("Distilled prompt: %s", distilled[:120])
        full_prompt = (config["context"] + "\n\n" + distilled) if config["context"] else distilled
    else:
        LOG.info("Claude distill failed — using original prompt for Devin")
        full_prompt = (config["context"] + "\n\n" + text) if config["context"] else text

    run_session(config, full_prompt, sender, state, blocking=blocking)


def run_migration(command_text, sender):
    """Run the Heroku migration runner in a background thread and text the result."""
    if not MIGRATE_SCRIPT.exists():
        send_reply(sender, "Migration runner not found. Check the repo.")
        return
    send_reply(sender, "Migration started; this may take a few minutes.")

    def worker():
        try:
            proc = subprocess.run(
                ["python3", str(MIGRATE_SCRIPT), command_text],
                capture_output=True,
                text=True,
                timeout=600,
            )
            output = (proc.stdout + "\n" + proc.stderr).strip()
            if not output:
                output = "Migration finished with no output."
            # iMessage can be long; send last ~2500 chars to keep it under chunk limits comfortably.
            if len(output) > 2500:
                output = "..." + output[-2497:]
            send_reply(sender, output)
        except Exception as e:
            send_reply(sender, f"Migration error: {e}")

    threading.Thread(target=worker, daemon=True).start()


def handle_command(config, text, sender, state, *, blocking=False):
    t = text.strip()
    lower = t.lower()

    if lower in ("new", "/new", "reset", "/reset", "new session", "reset session") or lower.startswith(("new ", "/new ", "reset ", "/reset ")):
        _clear_active_session(state, sender)
        # Start a fresh session immediately so the user gets a URL right away.
        kickoff = (config["context"] + "\n\nNew session started. Greet the user briefly and ask what you can help with.") if config["context"] else "New session started. Greet the user briefly and ask what you can help with."
        run_session(config, kickoff, sender, state, blocking=blocking)
        return True

    # Natural-language project creation prompts go to the builder pipeline.
    if lower.startswith(("create ", "build ", "make ", "design ", "scaffold ")):
        if blocking:
            builder.build(t, sender)
        else:
            send_reply(sender, builder.start_build(t, sender))
        return True

    if lower == "status":
        status = devin_usage.build_usage_status()
        send_reply(sender, devin_usage.format_status(status))
        return True

    if lower.startswith("estimate "):
        rest = t[9:].strip()
        size = None
        m = re.match(r"(?:--size\s+(small|medium|large)\s+)?(.*)", rest, re.I)
        if m:
            size = m.group(1)
            task = m.group(2)
        else:
            task = rest
        result = devin_usage.estimate_task(task, size)
        send_reply(sender, devin_usage.format_estimate(result))
        return True

    if lower.startswith("session "):
        session_id = t[8:].strip()
        send_reply(sender, devin_usage.get_session(session_id))
        return True

    if lower.startswith("migrate"):
        run_migration(t, sender)
        return True

    if lower == "url":
        active = _get_active_session(state, sender)
        if active:
            send_reply(sender, f"Devin Instance: {active['url']}")
        else:
            send_reply(sender, "No active session.")
        return True

    if lower in ("update", "deploy", "pull"):
        threading.Thread(target=_do_update, args=(sender,), daemon=True).start()
        return True

    return False


def handle_message(config, msg, state, *, blocking=False):
    text = (msg.get("text") or "").strip()
    sender = config.get("reply_to") or msg["handle"]
    sender_norm = imessage.normalize_handle(sender)
    pending = state.setdefault("pending", {})

    if handle_command(config, text, sender, state, blocking=blocking):
        return

    approve_key = f"{sender_norm}:approve"
    if approve_key in pending:
        if text.lower() in ("go", "yes", "y"):
            prompt = pending.pop(approve_key)
            save_state(state)
            send_reply(sender, "Running now.")
            full_prompt = (config["context"] + "\n\n" + prompt) if config["context"] else prompt
            run_session(config, full_prompt, sender, state, blocking=blocking)
        else:
            pending.pop(approve_key, None)
            save_state(state)
            send_reply(sender, "Approval cancelled. Send a new prompt when ready.")
        return

    if config["usage_guard"]:
        estimate = devin_usage.estimate_task(text)
        if estimate.get("would_exceed"):
            pending[approve_key] = text
            save_state(state)
            reply = (
                f"This looks like a {estimate['size']} task (~{estimate['estimate_sessions']:.0f} session, "
                f"~{estimate['estimate_acus']:.0f} ACUs) and would exceed one of your configured limits. "
                f"Reply GO to run anyway."
            )
            send_reply(sender, reply)
            return

    # If there's an active Devin session for this sender, treat the message as a follow-up.
    active = _get_active_session(state, sender)
    if active:
        res = send_message_to_session(config, active["session_id"], text)
        if res["ok"]:
            _start_poll(config, active["session_id"], sender, state, blocking)
            return
        # Session likely finished or errored; clear and fall through to router.
        _clear_active_session(state, sender)

    # New message with no active session — route through Claude:
    # general questions → Claude answers directly (free, no ACUs used)
    # coding tasks → Claude distills the prompt → Devin executes
    route_message(config, text, sender, state, blocking=blocking)


def main():
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    # Only log to stdout — launchd's StandardOutPath already routes that to
    # bridge.log. Adding a FileHandler here would double every line.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        stream=sys.stdout,
    )
    config = get_config()
    preflight(config)
    LOG.info("Bridge (re)starting. PID %d. Allowed senders: %s", os.getpid(), config["allowed_senders"])

    state = load_state()

    if "last_rowid" not in state:
        # First-ever run: skip all existing messages so old synced texts
        # don't replay. On subsequent restarts we resume from saved state.
        fresh_rowid = current_max_rowid()
        state["last_rowid"] = fresh_rowid
        save_state(state)
        LOG.debug("First run: set watermark to ROWID %d.", fresh_rowid)

    # Deduplicate allowed_senders so the same person listed with and without
    # the US country code (e.g. "+12035859184" and "203-585-9184") only gets
    # one startup ping. Strip a leading "1" from 11-digit numbers to normalize.
    def _dedup_key(h):
        n = imessage.normalize_handle(h)
        if len(n) == 11 and n.startswith("1"):
            n = n[1:]
        return n

    LOG.info("Bridge online.")

    while True:
        try:
            # Use the highest of: saved watermark, post-send rowid (in-memory),
            # and the persisted sent watermark — so we never re-process our own
            # outgoing replies (is_from_me=1) even across restarts.
            with _post_send_lock:
                effective_rowid = max(
                    state.get("last_rowid", 0),
                    _read_sent_watermark(),
                    _post_send_rowid,
                )
            messages = get_new_messages(effective_rowid, config["allowed_normalized"])
            if messages:
                new_last = max(m["rowid"] for m in messages)
                state["last_rowid"] = new_last
                save_state(state)
                for msg in messages:
                    LOG.info("New message from %s: %s", msg["handle"], msg["text"][:80])
                    # Run each handler in its own thread so the poll loop is
                    # never blocked by Devin API calls or osascript sends.
                    threading.Thread(
                        target=handle_message,
                        args=(config, msg, state),
                        daemon=True,
                    ).start()
        except Exception as e:
            LOG.exception("Main loop error: %s", e)
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
