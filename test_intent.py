"""Quick checks for the local reminder/note intent detection in bridge.py."""
import importlib.util
import sys
from pathlib import Path

# Import bridge.py without running main() or requiring macOS-only paths.
spec = importlib.util.spec_from_file_location("bridge", Path(__file__).with_name("bridge.py"))
bridge = importlib.util.module_from_spec(spec)
sys.modules["bridge"] = bridge
spec.loader.exec_module(bridge)

CASES = [
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


def main():
    failures = []
    for text, expected in CASES:
        got = bridge._detect_local_intent(text)
        status = "OK  " if got == expected else "FAIL"
        if got != expected:
            failures.append((text, expected, got))
        print(f"{status} {text!r} -> {got}")
    print()
    if failures:
        print(f"{len(failures)} failure(s)")
        return 1
    print(f"all {len(CASES)} cases passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
