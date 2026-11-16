"""Mint a signed bearer token from an ed25519 private key file.

    uv run src/mint.py keys/<created>_<expires>.private.pem [--ttl 10m] [--uses N | --unlimited]

Single-use by default. The token goes to stdout alone, so it pipes cleanly
(e.g. `| pbcopy`); details go to stderr. Token expiry is capped at the key's
expiry, since the server stops accepting a key once it expires.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from tokens import iso, parse_duration, parse_key_filename, sign_token


def positive_int(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("private_key", type=Path, help="path to a <created>_<expires>.private.pem file")
    parser.add_argument("--ttl", default="10m", help="token lifetime, e.g. 30s, 10m, 1h (default 10m)")
    uses = parser.add_mutually_exclusive_group()
    uses.add_argument("--uses", type=positive_int, default=1, help="number of allowed uses (default 1)")
    uses.add_argument("--unlimited", action="store_true", help="any number of uses until expiry")
    args = parser.parse_args()

    try:
        ttl = parse_duration(args.ttl)
    except ValueError as e:
        parser.error(str(e))

    key_file = parse_key_filename(args.private_key)
    if key_file is None or key_file.kind != "private":
        parser.error("key file name must look like <created>_<expires>.private.pem")

    now = int(time.time())
    if now >= key_file.expires:
        print(f"key {key_file.kid} expired at {iso(key_file.expires)}; run keygen.py", file=sys.stderr)
        return 1

    try:
        private_key = serialization.load_pem_private_key(args.private_key.read_bytes(), password=None)
    except (OSError, ValueError, TypeError) as e:
        print(f"cannot load {args.private_key}: {e}", file=sys.stderr)
        return 1
    if not isinstance(private_key, Ed25519PrivateKey):
        print(f"{args.private_key} is not an ed25519 private key", file=sys.stderr)
        return 1

    exp = now + ttl
    if exp > key_file.expires:
        exp = key_file.expires
        print("note: ttl capped at key expiry", file=sys.stderr)

    max_uses = None if args.unlimited else args.uses
    token, claims = sign_token(private_key, key_file.kid, exp, max_uses, now)

    uses_text = "unlimited uses" if max_uses is None else f"{max_uses} use{'s' if max_uses > 1 else ''}"
    print(f"token jti={claims.jti}: {uses_text}, expires {iso(exp)}", file=sys.stderr)
    print(token)
    return 0


if __name__ == "__main__":
    sys.exit(main())
