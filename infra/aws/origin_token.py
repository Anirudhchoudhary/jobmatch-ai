"""Add ORIGIN_VERIFY_TOKEN to the prod secret JSON if it is missing. Never prints a value.

Usage: origin_token.py <current secret JSON file> <merged output file>

Prints "present" (a usable token exists; nothing is written) or "added" (a new
`secrets.token_urlsafe(48)` token was merged into the existing keys and written, mode 600, to
the output file for `put-secret-value`). A missing, empty or short token is replaced, never
left empty. Exits 1 with a message that names no value if the secret is not a JSON object.
Run with `python3 -I`; standard library only.
"""

import json
import os
import secrets
import sys

KEY = "ORIGIN_VERIFY_TOKEN"
MIN_LEN = 32  # the API refuses a shorter token in prod


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: origin_token.py <secret.json> <merged.json>", file=sys.stderr)
        return 2
    src, out = argv
    try:
        with open(src, encoding="utf-8") as fh:
            current = json.load(fh)
    except (OSError, ValueError):
        print("error: the secret is not readable JSON", file=sys.stderr)
        return 1
    if not isinstance(current, dict):
        print("error: the secret must be a JSON object", file=sys.stderr)
        return 1
    token = current.get(KEY)
    if isinstance(token, str) and len(token) >= MIN_LEN:
        print("present")
        return 0
    merged = {**current, KEY: secrets.token_urlsafe(48)}
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(merged, fh)
    print("added")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
