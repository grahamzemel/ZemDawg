"""Quick checks for message routing helpers in bridge.py.

Covers reminder/note intent detection, the conversational-vs-project routing
guard, and the Claude-free fallback reminder-time parser.
Run with: python3 test_intent.py
"""
import importlib.util
import sys
from datetime import datetime
from pathlib import Path

# Import bridge.py without running main() or requiring macOS-only paths.
spec = importlib.util.spec_from_file_location("bridge", Path(__file__).with_name("bridge.py"))
bridge = importlib.util.module_from_spec(spec)
sys.modules["bridge"] = bridge
spec.loader.exec_module(bridge)

# Messages the bridge must handle itself rather than forwarding to Devin.
INTENT_CASES = [
    # reminders
    ("Remind me of the things I have to do at 5pm AT 5pm today:", "reminder"),
    ("Tomorrow at 5pm remind me trash, dishes, text Mari, clean", "reminder"),
    ("Text me HAIRCUT at 8am tmrw", "reminder"),
    ("Remind me of this in 30 mins:", "reminder"),
    ("11am tmrw morning alarm, dispo run", "reminder"),
    ("8:30pm dinner with Mari", "reminder"),
    ("at 5pm pick up keys from leasing office", "reminder"),
    ("tomorrow at 6pm get bar table in Longmont", "reminder"),
    ("set a reminder for the dentist", "reminder"),
    ("alarm for the dispo run", "reminder"),
    # notes
    ("Make a note of Legion overlook", "note"),
    ("jot this down: Cuddlefish has good hand rolls", "note"),
    ("note to self, charge the batteries", "note"),
    # neither
    ("build a parkour game for kids", None),
    ("How far is Legion Overlook from Pearl Parkway?", None),
    ("fix the bug in server.js", None),
    ("text me the summary", None),
    ("what time does the 5pm show start", None),
    ("Cuddlefish tmrw has good hand rolls", None),
    ("deploy the api and run the tests", None),
    ("can you refactor handle_message in bridge.py", None),
]

# True = should be routed to the project builder.
BUILD_CASES = [
    ("create a website for my band", True),
    ("build a parkour game for kids", True),
    ("make a parkour game", True),
    ("make me a todo app with svelte", True),
    ("make me a chrome extension", True),
    ("build a todo app with svelte", True),
    ("create a dashboard for my projects", True),
    ("scaffold an express api", True),
    ("scaffold a fastapi backend", True),
    ("design a landing page", True),
    # Conversational or acting on existing work — must not trigger a build.
    ("make it so i dont have to approve reminders", False),
    ("make it so I dont need to confirm stuff going forward", False),
    ("make it easier to deploy", False),
    ("make sure the tests pass", False),
    ("make that change to the navbar", False),
    ("make a note of Legion overlook", False),
    ("make yourself able to text me", False),
    ("build that out later", False),
    ("create the PR when you're done", False),
    ("what should i build", False),
]

# Fallback time parsing, evaluated against a fixed "now" of Mon 2026-07-20 08:30 local.
NOW = datetime(2026, 7, 20, 8, 30)
TIME_CASES = [
    ("remind me at 5pm to pick up keys", datetime(2026, 7, 20, 17, 0)),
    ("11am tmrw morning alarm, dispo run", datetime(2026, 7, 21, 11, 0)),
    ("text me HAIRCUT at 8am tmrw", datetime(2026, 7, 21, 8, 0)),
    ("remind me at 8:45pm about dinner", datetime(2026, 7, 20, 20, 45)),
    ("remind me in 30 mins", datetime(2026, 7, 20, 9, 0)),
    ("remind me in 2 hours", datetime(2026, 7, 20, 10, 30)),
    # 7am already passed today, so it means tomorrow.
    ("remind me at 7am to stretch", datetime(2026, 7, 21, 7, 0)),
    ("remind me about the thing", None),
]


def check(kind, text, expected, got, failures):
    ok = got == expected
    if not ok:
        failures.append((kind, text, expected, got))
    print(f"{'OK  ' if ok else 'FAIL'} {kind:7} {text!r} -> {got}")


def main():
    failures = []

    for text, expected in INTENT_CASES:
        check("intent", text, expected, bridge._detect_local_intent(text), failures)

    print()
    for text, expected in BUILD_CASES:
        check("build", text, expected, bridge._looks_like_build_request(text), failures)

    print()
    for text, expected in TIME_CASES:
        ts = bridge._parse_reminder_time(text, now=NOW)
        got = datetime.fromtimestamp(ts) if ts else None
        check("time", text, expected, got, failures)

    total = len(INTENT_CASES) + len(BUILD_CASES) + len(TIME_CASES)
    print()
    if failures:
        print(f"{len(failures)} of {total} failed")
        for kind, text, expected, got in failures:
            print(f"  {kind}: {text!r} expected {expected}, got {got}")
        return 1
    print(f"all {total} cases passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
