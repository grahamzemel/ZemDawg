# Devin iMessage Bridge

Python daemons that turn the Mac mini into a Devin controller over iMessage.

## Files

- `bridge.py` — polls `~/Library/Messages/chat.db`, dispatches allowed senders to Devin Cloud or the local builder, and texts back session URLs, PR links, or build results.
- `builder.py` — project builder pipeline. Receives natural-language prompts (e.g. "Create a website..."), runs the local `claude` CLI to scaffold frontend + backend, installs/builds, and optionally deploys to Netlify and exposes the backend through a Cloudflare Tunnel.
- `mock_bridge.py` — same routing logic as the real bridge but emits NDJSON to stdout instead of sending iMessages. Useful for testing the builder/bridge from a web UI or the terminal. Run with `--server` to keep a single long-lived process for multi-turn testing.
- `CONTEXT.md` — default prompt context the bridge prepends when starting a new Devin session. Edit this instead of embedding a huge prompt in `secrets/devin.env`.
- `devin_usage.py` — CLI for status, estimate, and session lookup. Also used by `bridge.py` for usage guard (session counts + optional ACU cap).
- `imessage.py` — tiny helper for normalizing handles and sending iMessages via AppleScript.
- `claude_cli.py` — locates the Claude Code CLI at runtime (bundled extension under `~/.devin-server`, then `claude` on PATH, then common install paths). Override with `CLAUDE_CLI` / `CLAUDE_NODE`. Never hardcode a versioned path — the extension directory changes on every Claude Code update.
- `test_intent.py` — checks for message routing: reminder/note intent detection, build-request detection, and the fallback reminder-time parser. Run with `python3 test_intent.py`.
- `watchdog.py` — `launchd` job that texts you at 50%, 80%, and 100% of `DEVIN_DAILY_SESSION_LIMIT`, `DEVIN_WEEKLY_SESSION_LIMIT`, or `DEVIN_MONTHLY_ACU_QUOTA` when set.
- `com.devin.imessagebridge.plist.template` and `com.devin.acuwatchdog.plist.template` — launchd templates rendered by `../scripts/render-plists.py`.

## Commands

Text the mini:

- `status` — usage (sessions today/this week/this cycle + optional daily/weekly/session + monthly ACU cap)
- `estimate <task>` or `estimate --size large <task>` — session + ACU estimate
- `session devin-abc123` — session details
- `migrate ...` — runs the migration runner in `../migration/`
- `create ...` / `build ...` / `design ...` — runs the local builder to generate a full-stack project (Svelte/Tailwind frontend + Node backend by default), install/build, and optionally deploy to Netlify + Cloudflare Tunnel
- `new` or `reset` — clears the active Devin session for your handle so the next message starts a fresh chain with the full `CONTEXT.md` prompt
- `reminders` — list pending reminders; `cancel reminder <n>` cancels one
- `notes` — list saved notes
- anything else — sent to the active Devin session as a follow-up; if there is no active session, a new Devin session is created and the context from `CONTEXT.md` is prepended once

## Reminders and notes

Reminder and note messages are always handled locally by the bridge, **before** the
active-session follow-up path in `handle_message`. This matters: once a Devin session is
active, every other message is forwarded to that session as a follow-up, so a reminder
routed that way would never reach the scheduler and would never fire.

`_check_reminders()` runs on every poll cycle and texts any reminder whose time has come.
Time extraction prefers Claude, but falls back to `_parse_reminder_time()` so reminders
still get set when the Claude CLI is unavailable.

Phrasings that are recognised: `remind me ...`, `text me X at 8am`, `set a reminder ...`,
anything containing `alarm`, a leading clock time (`11am tmrw dispo run`, `8:30pm dinner`),
and a leading day word plus a clock time (`tomorrow at 5pm trash, dishes`). Add new
phrasings to `_REMINDER_INTENT_RE` and cover them in `test_intent.py`.

## Permissions

- System Settings → Privacy & Security → Full Disk Access → add `/usr/bin/python3` (or Homebrew Python) so the bridge can read the Messages database.
- The first reply will prompt to allow controlling the Messages app.

## Running manually

```bash
cd bridge
set -a; source ../secrets/devin.env; set +a
python3 bridge.py
```

## Logs

`bridge/logs/bridge.log` and `bridge/logs/watchdog.log` are written once the plists are loaded.

Tail live logs:

```bash
tail -f ~/code/zemdawg/logs/bridge.log
```
