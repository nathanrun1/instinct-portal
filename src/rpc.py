"""JSON-RPC error codes and the exception method handlers raise."""

from __future__ import annotations

from typing import Any

# Standard JSON-RPC 2.0 codes
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

# Implementation-defined server errors (-32000 to -32099)
UNAUTHORIZED = -32001


class RpcError(Exception):
    """Raised by a method handler; becomes the JSON-RPC `error` object."""

    def __init__(self, code: int, message: str, data: Any = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data
