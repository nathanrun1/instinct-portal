"""JSON-RPC endpoint for Instinct AI.

    uv run src/main.py [--host 127.0.0.1] [--port 8000] [--keys-dir keys] [--db data/used.sqlite]

POST /rpc with `Authorization: Bearer <token>` and a JSON-RPC 2.0 request body.
The server holds public keys only (keys/*.public.pem, created by keygen.py) and
rescans the directory when it sees an unknown key ID, so new keys work without
a restart.

Expose it publicly through an HTTPS tunnel, e.g.
    cloudflared tunnel --url http://localhost:8000
"""

from __future__ import annotations

import argparse
import inspect
import json
import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable

import uvicorn
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from rpc import (
    INTERNAL_ERROR,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    UNAUTHORIZED,
    RpcError,
)
from tokens import (
    DEFAULT_KEYS_DIR,
    PROJECT_ROOT,
    Claims,
    TokenError,
    iso,
    parse_key_filename,
    parse_token,
    verify_signature,
)
from vault import Vault

log = logging.getLogger("portal")

DEFAULT_DB = PROJECT_ROOT / "data" / "used.sqlite"
DEFAULT_VAULT_URL = "http://127.0.0.1:8080"
MAX_BODY_BYTES = 1_000_000
MAX_LOGGED_PAYLOAD = 2000  # characters; keeps note contents from flooding the log


# --- key store --------------------------------------------------------------


class KeyStore:
    """Public keys from keys_dir, indexed by kid (the key's created timestamp).

    Deleting a .public.pem file revokes that key immediately.
    """

    def __init__(self, keys_dir: Path):
        self.keys_dir = keys_dir
        self._keys: dict[str, tuple[Ed25519PublicKey, int]] = {}
        self._paths: dict[str, Path] = {}

    def scan(self) -> None:
        if not self.keys_dir.is_dir():
            return
        for path in self.keys_dir.glob("*.public.pem"):
            key_file = parse_key_filename(path)
            if key_file is None or key_file.kid in self._keys:
                continue
            try:
                key = serialization.load_pem_public_key(path.read_bytes())
            except (OSError, ValueError) as e:
                log.warning("skipping %s: %s", path.name, e)
                continue
            if not isinstance(key, Ed25519PublicKey):
                log.warning("skipping %s: not an ed25519 key", path.name)
                continue
            self._keys[key_file.kid] = (key, key_file.expires)
            self._paths[key_file.kid] = path

    def get(self, kid: str) -> tuple[Ed25519PublicKey, int] | None:
        if kid in self._keys and not self._paths[kid].exists():
            log.info("key %s removed from disk; revoked", kid)
            del self._keys[kid], self._paths[kid]
        if kid not in self._keys:
            self.scan()
        return self._keys.get(kid)

    def valid(self, now: int) -> dict[str, int]:
        return {kid: exp for kid, (_, exp) in self._keys.items() if exp > now}


# --- use counter ------------------------------------------------------------


class UseStore:
    """Per-token use counts, persisted so a restart cannot revive a used token."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, isolation_level=None)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS uses ("
            " jti TEXT PRIMARY KEY, count INTEGER NOT NULL, exp INTEGER NOT NULL)"
        )

    def consume(self, claims: Claims, now: int) -> bool:
        """Record one use. False if the token has no uses left."""
        # Rows for expired tokens are no longer needed: those tokens fail the expiry check.
        self.conn.execute("DELETE FROM uses WHERE exp <= ?", (now,))
        row = self.conn.execute(
            "INSERT INTO uses (jti, count, exp) VALUES (?, 1, ?)"
            " ON CONFLICT(jti) DO UPDATE SET count = count + 1 WHERE count < ?"
            " RETURNING count",
            (claims.jti, claims.exp, claims.max_uses),
        ).fetchone()
        return row is not None


# --- authentication ---------------------------------------------------------


def authenticate(header: str | None, keys: KeyStore, uses: UseStore, now: int) -> Claims:
    if not header:
        raise TokenError("missing_token")
    scheme, _, token = header.partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token:
        raise TokenError("malformed_token")

    claims, signed, signature = parse_token(token)
    entry = keys.get(claims.kid)
    if entry is None:
        raise TokenError("unknown_key")
    public_key, key_expires = entry
    if now >= key_expires:
        raise TokenError("key_expired")
    verify_signature(public_key, signed, signature)

    # Claims are authentic from here on, so the jti is safe to log.
    if now >= claims.exp:
        raise TokenError("token_expired", claims.jti)
    if claims.max_uses is not None and not uses.consume(claims, now):
        raise TokenError("token_used", claims.jti)
    return claims


# --- JSON-RPC ---------------------------------------------------------------


def ping(params: Any) -> str:
    return "pong"


# Handlers take the request's params and return a result (or an awaitable of
# one), or raise RpcError.
Methods = dict[str, Callable[[Any], Any]]
BASE_METHODS: Methods = {
    "ping": ping,
}


def rpc_error(id_: Any, code: int, message: str, data: Any = None) -> dict:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": id_, "error": error}


def invalid_reason(msg: Any) -> str | None:
    if isinstance(msg, list):
        return "batch requests are not supported"
    if not isinstance(msg, dict):
        return "request must be a JSON object"
    if msg.get("jsonrpc") != "2.0":
        return 'jsonrpc must be "2.0"'
    if not isinstance(msg.get("method"), str):
        return "method must be a string"
    if "params" in msg and not isinstance(msg["params"], (dict, list)):
        return "params must be an object or array"
    if "id" in msg and not valid_id(msg["id"]):
        return "id must be a string, number or null"
    return None


def valid_id(id_: Any) -> bool:
    return id_ is None or (isinstance(id_, (str, int, float)) and not isinstance(id_, bool))


async def read_body(request: Request, limit: int) -> bytes | None:
    """Read the body, or return None once it exceeds `limit` bytes."""
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        return None
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


async def dispatch(methods: Methods, msg: dict, jti: str) -> dict:
    id_ = msg.get("id")
    handler = methods.get(msg["method"])
    if handler is None:
        return rpc_error(id_, METHOD_NOT_FOUND, "method not found")
    try:
        result = handler(msg.get("params"))
        if inspect.isawaitable(result):
            result = await result
    except RpcError as e:
        detail = "" if e.data is None else " " + truncate(json.dumps(e.data, ensure_ascii=False), 300)
        log.info("jti=%s %s error %d: %s%s", jti, msg["method"], e.code, e.message, detail)
        return rpc_error(id_, e.code, e.message, e.data)
    except Exception:
        log.exception("jti=%s handler %s failed", jti, msg["method"])
        return rpc_error(id_, INTERNAL_ERROR, "internal error")
    return {"jsonrpc": "2.0", "id": id_, "result": result}


def truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else f"{text[:limit]}... [{len(text) - limit} more chars]"


def create_app(keys: KeyStore, uses: UseStore, methods: Methods) -> Starlette:
    async def rpc(request: Request) -> Response:
        now = int(time.time())
        try:
            claims = authenticate(request.headers.get("authorization"), keys, uses, now)
        except TokenError as e:
            log.warning("auth failed: %s%s", e.reason, f" jti={e.jti}" if e.jti else "")
            return JSONResponse(
                rpc_error(None, UNAUTHORIZED, "unauthorized", {"reason": e.reason}),
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )

        body = await read_body(request, MAX_BODY_BYTES)
        if body is None:
            log.warning("jti=%s body over %d bytes", claims.jti, MAX_BODY_BYTES)
            return JSONResponse(rpc_error(None, INVALID_REQUEST, "request too large"), status_code=413)

        try:
            msg = json.loads(body)
        except (ValueError, RecursionError):
            log.info("jti=%s unparseable payload: %r", claims.jti, body[:500])
            return JSONResponse(rpc_error(None, PARSE_ERROR, "parse error"))

        payload = json.dumps(msg, separators=(",", ":"), ensure_ascii=False)
        log.info("jti=%s payload: %s", claims.jti, truncate(payload, MAX_LOGGED_PAYLOAD))

        reason = invalid_reason(msg)
        if reason:
            id_ = msg.get("id") if isinstance(msg, dict) and valid_id(msg.get("id")) else None
            return JSONResponse(rpc_error(id_, INVALID_REQUEST, "invalid request", reason))

        response = await dispatch(methods, msg, claims.jti)
        if "id" not in msg:  # notification: run it, return nothing
            return Response(status_code=204)
        return JSONResponse(response)

    return Starlette(routes=[Route("/rpc", rpc, methods=["POST"])])


def find_vault_token_file(explicit: Path | None) -> Path | None:
    """--vault-token-file if given, else systemd's LoadCredential=vault-token:..."""
    if explicit is not None:
        if not explicit.is_file():
            raise SystemExit(f"--vault-token-file {explicit} does not exist")
        return explicit
    creds = os.environ.get("CREDENTIALS_DIRECTORY")
    if creds and (Path(creds) / "vault-token").is_file():
        return Path(creds) / "vault-token"
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--keys-dir", type=Path, default=DEFAULT_KEYS_DIR)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--vault-url", default=DEFAULT_VAULT_URL,
                        help=f"vault sync server on this machine (default {DEFAULT_VAULT_URL})")
    parser.add_argument("--vault-token-file", type=Path,
                        help="file holding the sync server device token; defaults to the systemd "
                             "credential 'vault-token' if present. Without a token, vault.* is disabled.")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # per-request lines duplicate the payload log

    keys = KeyStore(args.keys_dir)
    keys.scan()
    valid = keys.valid(int(time.time()))
    if valid:
        for kid, exp in sorted(valid.items()):
            log.info("key %s valid until %s", kid, iso(exp))
    else:
        log.warning("no unexpired public keys in %s; run keygen.py (no restart needed)", args.keys_dir)

    if args.host not in ("127.0.0.1", "localhost", "::1"):
        log.warning("binding %s directly: bearer tokens travel unencrypted without a TLS proxy", args.host)

    methods = dict(BASE_METHODS)
    token_file = find_vault_token_file(args.vault_token_file)
    if token_file is None:
        log.info("vault.* disabled: no vault token file")
    else:
        methods.update(Vault(args.vault_url, token_file.read_text().strip()).methods())
        log.info("vault.* enabled: sync server at %s", args.vault_url)

    app = create_app(keys, UseStore(args.db), methods)
    uvicorn.run(app, host=args.host, port=args.port, log_config=None, server_header=False)


if __name__ == "__main__":
    main()
