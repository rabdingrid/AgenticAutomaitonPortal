#!/usr/bin/env python3
"""Generate a bcrypt hash for a new user password.

Usage (from backend/):
    .venv/bin/python scripts/hash_password.py mySecretPassword

Then paste the printed hash into users.json under hashed_password.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from auth import hash_password  # noqa: E402


def main() -> None:
    if len(sys.argv) != 2:
        print("Usage: python hash_password.py <plain-password>")
        sys.exit(1)
    print(hash_password(sys.argv[1]))


if __name__ == "__main__":
    main()
