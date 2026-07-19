# Devin iMessage Bridge

Python daemons that turn the Mac mini into a Devin controller over iMessage.

## Files

- `bridge.py` — polls `~/Library/Messages/chat.db`, dispatches allowed senders to Devin Cloud or the local builder, and texts back session URLs, PR links, or build results.
- `builder.py` — project builder pipeline. Receives natural-language prompts (e.g. "Create a website..."), runs the local `claude` CLI to scaffold frontend + backend, installs/builds, and optionally deploys to Netlify and exposes the backend through a Cloudflare Tunnel.
- `mock_bridge.py` — same routing logic as the real bridge but emits NDJSON to stdout instead of sending iMessages. Useful for testing the builder/bridge from a web UI or the terminal. Run with `--server` to keep a single long-lived process for multi-turn testing.
- `CONTEXT.md` — default prompt context the bridge prepends when starting a new Devin session. Edit this instead of embedding a huge prompt in `secrets/devin.env`.
- `devin_usage.py` — CLI for status, estimate, and session lookup. Also used by `bridge.py` for usage guard (session counts + optional ACU cap).
- `imessage.py` — tiny helper for normalizing handles and sending iMessages via AppleScript.
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
- anything else — sent to the active Devin session as a follow-up; if there is no active session, a new Devin session is created and the context from `CONTEXT.md` is prepended once

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
