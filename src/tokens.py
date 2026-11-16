"""Token format and key-file conventions shared by the server and the CLIs.

Token
    base64url(claims_json) "." base64url(ed25519_signature)

    The signature covers the ASCII bytes of the first segment exactly as sent,
    so verification never depends on re-serializing JSON.

    Claims: {"kid": str, "jti": str, "iat": int, "exp": int, "max_uses": int | null}
    max_uses = 1 is single-use; N allows N uses before exp; null allows unlimited
    uses before exp.

Key files
    <created>_<expires>.public.pem and <created>_<expires>.private.pem, with UTC
    timestamps formatted YYYYMMDDTHHMMSSZ. The key ID (kid) is the created
    timestamp.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_KEYS_DIR = PROJECT_ROOT / "keys"

TS_FORMAT = "%Y%m%dT%H%M%SZ"
_TS = r"\d{8}T\d{6}Z"
KID_RE = re.compile(rf"^{_TS}$")
KEY_FILE_RE = re.compile(
    rf"^(?P<created>{_TS})_(?P<expires>{_TS})\.(?P<kind>public|private)\.pem$"
)
_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_DURATION_RE = re.compile(r"^(\d+)([smhd])$")
_DURATION_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}

MAX_TOKEN_LEN = 2048
SIGNATURE_LEN = 64


# --- time and durations -----------------------------------------------------


def format_ts(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime(TS_FORMAT)


def parse_ts(text: str) -> int:
    return int(datetime.strptime(text, TS_FORMAT).replace(tzinfo=timezone.utc).timestamp())


def iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="seconds")


def parse_duration(text: str) -> int:
    """Parse '30s', '10m', '24h' or '7d' into seconds."""
    match = _DURATION_RE.match(text.strip())
    if not match or int(match.group(1)) == 0:
        raise ValueError(f"invalid duration {text!r}; use e.g. 30s, 10m, 24h, 7d")
    return int(match.group(1)) * _DURATION_UNITS[match.group(2)]


# --- key files --------------------------------------------------------------


@dataclass(frozen=True)
class KeyFile:
    path: Path
    kid: str
    created: int
    expires: int
    kind: str  # "public" or "private"


def key_filename(created: int, expires: int, kind: str) -> str:
    return f"{format_ts(created)}_{format_ts(expires)}.{kind}.pem"


def parse_key_filename(path: Path) -> KeyFile | None:
    match = KEY_FILE_RE.match(path.name)
    if not match:
        return None
    return KeyFile(
        path=path,
        kid=match["created"],
        created=parse_ts(match["created"]),
        expires=parse_ts(match["expires"]),
        kind=match["kind"],
    )


# --- tokens -----------------------------------------------------------------


class TokenError(Exception):
    """Authentication failure. `reason` is a stable code safe to return to clients."""

    def __init__(self, reason: str, jti: str | None = None):
        super().__init__(reason)
        self.reason = reason
        self.jti = jti  # set only once the claims are known to be authentic


@dataclass(frozen=True)
class Claims:
    kid: str
    jti: str
    iat: int
    exp: int
    max_uses: int | None


def _b64e(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def sign_token(
    private_key: Ed25519PrivateKey,
    kid: str,
    exp: int,
    max_uses: int | None,
    now: int | None = None,
) -> tuple[str, Claims]:
    iat = int(time.time()) if now is None else now
    claims = Claims(kid=kid, jti=secrets.token_urlsafe(12), iat=iat, exp=exp, max_uses=max_uses)
    payload = json.dumps(claims.__dict__, separators=(",", ":")).encode()
    signed = _b64e(payload)
    signature = private_key.sign(signed.encode("ascii"))
    return f"{signed}.{_b64e(signature)}", claims


def parse_token(token: str) -> tuple[Claims, bytes, bytes]:
    """Decode a token without verifying it. Returns (claims, signed_bytes, signature)."""
    if len(token) > MAX_TOKEN_LEN or token.count(".") != 1:
        raise TokenError("malformed_token")
    signed, sig_text = token.split(".")
    if not (_SEGMENT_RE.match(signed) and _SEGMENT_RE.match(sig_text)):
        raise TokenError("malformed_token")
    try:
        raw = json.loads(_b64d(signed))
        signature = _b64d(sig_text)
    except (binascii.Error, ValueError, RecursionError):
        raise TokenError("malformed_token") from None

    if not isinstance(raw, dict) or set(raw) != {"kid", "jti", "iat", "exp", "max_uses"}:
        raise TokenError("malformed_token")
    max_uses = raw["max_uses"]
    if not (
        isinstance(raw["kid"], str)
        and KID_RE.match(raw["kid"])
        and isinstance(raw["jti"], str)
        and 0 < len(raw["jti"]) <= 64
        and _is_int(raw["iat"])
        and _is_int(raw["exp"])
        and raw["exp"] > raw["iat"]
        and (max_uses is None or (_is_int(max_uses) and max_uses >= 1))
        and len(signature) == SIGNATURE_LEN
    ):
        raise TokenError("malformed_token")
    return Claims(**raw), signed.encode("ascii"), signature


def verify_signature(public_key: Ed25519PublicKey, signed: bytes, signature: bytes) -> None:
    try:
        public_key.verify(signature, signed)
    except InvalidSignature:
        raise TokenError("bad_signature") from None
