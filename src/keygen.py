"""Generate an ed25519 key pair with timestamped file names.

    uv run src/keygen.py [--ttl 24h] [--keys-dir keys]

Writes <created>_<expires>.private.pem (mode 600) and .public.pem, and deletes
expired pairs in the same directory. Prints the private key path on stdout, so
it composes with mint.py:

    uv run src/mint.py "$(uv run src/keygen.py)"

The server only needs the .public.pem file. If it runs on another machine, copy
just that file into its keys directory.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from tokens import DEFAULT_KEYS_DIR, format_ts, iso, key_filename, parse_duration, parse_key_filename


def write_new(path: Path, data: bytes, mode: int) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(fd, "wb") as f:
        f.write(data)


def prune_expired(keys_dir: Path, now: int) -> list[Path]:
    removed = []
    for path in sorted(keys_dir.iterdir()):
        key_file = parse_key_filename(path)
        if key_file and key_file.expires <= now:
            path.unlink()
            removed.append(path)
    return removed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--ttl", default="24h", help="key lifetime, e.g. 1h, 24h, 7d (default 24h)")
    parser.add_argument("--keys-dir", type=Path, default=DEFAULT_KEYS_DIR)
    args = parser.parse_args()

    try:
        ttl = parse_duration(args.ttl)
    except ValueError as e:
        parser.error(str(e))

    args.keys_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    now = int(time.time())
    for path in prune_expired(args.keys_dir, now):
        print(f"removed expired {path.name}", file=sys.stderr)

    created, expires = now, now + ttl
    private_key = Ed25519PrivateKey.generate()
    private_pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    public_pem = private_key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )

    private_path = args.keys_dir / key_filename(created, expires, "private")
    public_path = args.keys_dir / key_filename(created, expires, "public")
    try:
        write_new(private_path, private_pem, 0o600)
        write_new(public_path, public_pem, 0o644)
    except FileExistsError:
        print("a key created this second already exists; retry in a second", file=sys.stderr)
        return 1

    print(f"key {format_ts(created)} valid until {iso(expires)}", file=sys.stderr)
    print(private_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
