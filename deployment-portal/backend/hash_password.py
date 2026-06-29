#!/usr/bin/env python3
"""Generate a bcrypt hash for a new user password.

Usage:
    python hash_password.py mySecretPassword

Then paste the printed hash into users.json under hashed_password.
"""

from __future__ import annotations

import sys

from passlib.context import CryptContext

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def main() -> None:
    if len(sys.argv) != 2:
        print("Usage: python hash_password.py <plain-password>")
        sys.exit(1)
    print(pwd_context.hash(sys.argv[1]))


if __name__ == "__main__":
    main()
