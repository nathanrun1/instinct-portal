"""vault.* methods: read/write access to an Obsidian vault through a
Self-Hosted Vault Sync server running on this machine.

Sync server: https://github.com/PeoneEr/self-hosted-vault-sync-server
Endpoints used: GET /changes, GET/PUT/DELETE /file/{path}.

Deliberately not exposed:
- POST /pair, GET /devices, DELETE /devices/{id}: /pair would let a caller mint
  permanent device tokens with full vault access, and revoke could lock out the
  user's own devices.
- GET /events: a long-lived stream, which does not fit one-shot RPC calls.
- Hidden paths (any segment starting with "."), e.g. .obsidian/: plugin code
  and settings live there, including the sync plugin's own device token, and
  a written plugin would run on every synced device.

Writes use the sync server's hash-based conflict detection: overwriting or
deleting an existing file requires the hash returned by vault.read, so the
agent cannot clobber an edit made on another device since it read the file.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
from typing import Any, Awaitable, Callable
from urllib.parse import quote

import httpx

from rpc import INVALID_PARAMS, RpcError

VAULT_UNAVAILABLE = -32010
NOT_FOUND = -32011
CONFLICT = -32012
FORBIDDEN_PATH = -32013
TOO_LARGE = -32014

MAX_READ_BYTES = 1_000_000
MAX_PATH_LEN = 1024
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


# --- params -----------------------------------------------------------------


def _params(params: Any, required: tuple[str, ...], optional: tuple[str, ...] = ()) -> dict:
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise RpcError(INVALID_PARAMS, "params must be an object")
    unknown = sorted(set(params) - set(required) - set(optional))
    if unknown:
        raise RpcError(INVALID_PARAMS, f"unknown params: {', '.join(unknown)}")
    missing = [k for k in required if k not in params]
    if missing:
        raise RpcError(INVALID_PARAMS, f"missing params: {', '.join(missing)}")
    return params


def _is_hidden(path: str) -> bool:
    return any(seg.startswith(".") for seg in path.split("/"))


def _check_path(path: Any) -> str:
    if not isinstance(path, str) or not path:
        raise RpcError(INVALID_PARAMS, "path must be a non-empty string")
    if (
        len(path) > MAX_PATH_LEN
        or path.startswith("/")
        or "\\" in path
        or _CONTROL_RE.search(path)
        or any(seg in ("", ".", "..") for seg in path.split("/"))
    ):
        raise RpcError(INVALID_PARAMS, "invalid path: use a relative path like 'folder/note.md'")
    if _is_hidden(path):
        raise RpcError(FORBIDDEN_PATH, "hidden paths (any part starting with '.') are not accessible")
    return path


def _check_hash(value: Any, name: str) -> str:
    if not isinstance(value, str) or not _HASH_RE.match(value):
        raise RpcError(INVALID_PARAMS, f"{name} must be a 64-character lowercase hex sha256")
    return value


def _check_int(value: Any, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise RpcError(INVALID_PARAMS, f"{name} must be a non-negative integer")
    return value


def _file_url(path: str) -> str:
    return "/file/" + quote(path, safe="/")


# --- client -----------------------------------------------------------------


def _unreachable(e: Exception) -> RpcError:
    return RpcError(VAULT_UNAVAILABLE, "vault sync server unreachable", f"{type(e).__name__}: {e}")


def _unexpected(resp: httpx.Response) -> RpcError:
    if resp.status_code == 401:
        return RpcError(VAULT_UNAVAILABLE, "vault sync server rejected the portal's device token")
    return RpcError(VAULT_UNAVAILABLE, f"vault sync server returned HTTP {resp.status_code}")


class Vault:
    def __init__(self, base_url: str, token: str, timeout: float = 15.0):
        self.base_url = base_url
        self.client = httpx.AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
        )

    def methods(self) -> dict[str, Callable[[Any], Awaitable[Any]]]:
        return {
            "vault.list": self.list,
            "vault.read": self.read,
            "vault.write": self.write,
            "vault.delete": self.delete,
            "vault.changes": self.changes,
        }

    async def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        try:
            return await self.client.request(method, url, **kwargs)
        except httpx.HTTPError as e:
            raise _unreachable(e) from None

    async def _fetch(self, path: str) -> tuple[str, int, bytes | None] | None:
        """(hash, size, data) for path, or None if it does not exist.

        Streams the body so large attachments are hashed without being held in
        memory; data is None when the file is over MAX_READ_BYTES.
        """
        try:
            async with self.client.stream("GET", _file_url(path)) as resp:
                if resp.status_code == 404:
                    return None
                if resp.status_code != 200:
                    raise _unexpected(resp)
                digest, size, chunks = hashlib.sha256(), 0, []
                async for chunk in resp.aiter_bytes():
                    digest.update(chunk)
                    size += len(chunk)
                    if size <= MAX_READ_BYTES:
                        chunks.append(chunk)
                data = b"".join(chunks) if size <= MAX_READ_BYTES else None
                return digest.hexdigest(), size, data
        except httpx.HTTPError as e:
            raise _unreachable(e) from None

    async def _changes(self, since: int) -> list[dict]:
        resp = await self._request("GET", "/changes", params={"since": since})
        if resp.status_code != 200:
            raise _unexpected(resp)
        entries = resp.json()
        if not isinstance(entries, list):
            raise RpcError(VAULT_UNAVAILABLE, "vault sync server returned an unexpected changelog")
        return [e for e in entries if isinstance(e, dict) and isinstance(e.get("path"), str)]

    # --- methods ----------------------------------------------------------------

    async def list(self, params: Any) -> dict:
        """All files, optionally under a path prefix. Rebuilt from the changelog,
        since the sync server has no listing endpoint."""
        p = _params(params, (), ("prefix",))
        prefix = p.get("prefix", "")
        if not isinstance(prefix, str):
            raise RpcError(INVALID_PARAMS, "prefix must be a string")

        latest: dict[str, dict] = {}
        for entry in await self._changes(0):
            latest[entry["path"]] = entry  # changelog is in append order: last entry wins
        files = [
            {"path": path, "hash": e.get("hash"), "modified": e.get("ts")}
            for path, e in sorted(latest.items())
            if e.get("action") != "deleted" and path.startswith(prefix) and not _is_hidden(path)
        ]
        return {"files": files, "count": len(files)}

    async def read(self, params: Any) -> dict:
        p = _params(params, ("path",))
        path = _check_path(p["path"])
        found = await self._fetch(path)
        if found is None:
            raise RpcError(NOT_FOUND, "file not found", {"path": path})
        digest, size, data = found
        if data is None:
            raise RpcError(TOO_LARGE, f"file is over {MAX_READ_BYTES} bytes", {"size": size, "hash": digest})
        try:
            return {"path": path, "hash": digest, "encoding": "utf-8", "content": data.decode("utf-8")}
        except UnicodeDecodeError:
            return {"path": path, "hash": digest, "encoding": "base64", "content": base64.b64encode(data).decode()}

    async def write(self, params: Any) -> dict:
        """Create a file, or overwrite one whose current hash matches base_hash."""
        p = _params(params, ("path", "content"), ("encoding", "base_hash"))
        path = _check_path(p["path"])
        content, encoding = p["content"], p.get("encoding", "utf-8")
        if not isinstance(content, str):
            raise RpcError(INVALID_PARAMS, "content must be a string")
        if encoding == "utf-8":
            data = content.encode("utf-8")
        elif encoding == "base64":
            try:
                data = base64.b64decode(content, validate=True)
            except binascii.Error:
                raise RpcError(INVALID_PARAMS, "content is not valid base64") from None
        else:
            raise RpcError(INVALID_PARAMS, 'encoding must be "utf-8" or "base64"')
        base_hash = p.get("base_hash")
        if base_hash is not None:
            _check_hash(base_hash, "base_hash")

        # Check before writing: on a mismatch the sync server would store the
        # content as a conflict copy that syncs to every device, rather than reject it.
        found = await self._fetch(path)
        current = found[0] if found else None
        if current is not None and base_hash is None:
            raise RpcError(CONFLICT, "file already exists: read it first and pass its hash as base_hash",
                           {"current_hash": current})
        if current != base_hash:
            message = "file no longer exists" if current is None else "file changed since it was read"
            raise RpcError(CONFLICT, message, {"current_hash": current})

        resp = await self._request("PUT", _file_url(path), content=data, headers={"X-Base-Hash": current or ""})
        if resp.status_code != 200:
            raise _unexpected(resp)
        if resp.headers.get("X-Conflict") == "true":
            # Another device wrote between the check above and the PUT.
            raise RpcError(CONFLICT, "file changed during the write; the new content was saved as a "
                           "separate conflict copy next to it", {"current_hash": resp.headers.get("X-Current-Hash")})
        return {"path": path, "hash": hashlib.sha256(data).hexdigest(), "created": current is None}

    async def delete(self, params: Any) -> dict:
        p = _params(params, ("path", "base_hash"))
        path = _check_path(p["path"])
        base_hash = _check_hash(p["base_hash"], "base_hash")
        resp = await self._request("DELETE", _file_url(path), headers={"X-Base-Hash": base_hash})
        if resp.status_code == 404:
            raise RpcError(NOT_FOUND, "file not found", {"path": path})
        if resp.status_code == 409:
            found = await self._fetch(path)
            raise RpcError(CONFLICT, "file changed since it was read", {"current_hash": found[0] if found else None})
        if resp.status_code != 200:
            raise _unexpected(resp)
        return {"path": path, "deleted": True}

    async def changes(self, params: Any) -> dict:
        """Changelog entries with ts > since (unix seconds)."""
        p = _params(params, ("since",))
        since = _check_int(p["since"], "since")
        changes = []
        for e in await self._changes(since):
            if _is_hidden(e["path"]):
                continue
            entry = {"ts": e.get("ts"), "path": e["path"], "action": e.get("action")}
            if e.get("hash"):
                entry["hash"] = e["hash"]
            changes.append(entry)
        return {"changes": changes}
