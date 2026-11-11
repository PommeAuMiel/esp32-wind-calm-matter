"""Small synchronous JSON WebSocket client for a trusted local HA endpoint.

Only the Python standard library is required. Importing this module has no side
effects. The caller supplies authentication messages and owns their contents.
Use one thread per client; TLS, proxies and WebSocket extensions are not used.
Receive timeouts preserve buffered frames, so a caller may wait and try again.
"""

import base64
import hashlib
import hmac
import json
import math
import os
import select
import socket
import struct
import time


MAX_MESSAGE_BYTES = 4 * 1024 * 1024
MAX_HEADER_BYTES = 64 * 1024
MAX_TIMEOUT_SECONDS = 60.0
_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class ProtocolError(ValueError):
    """The peer did not speak the supported WebSocket/JSON protocol."""


class ConnectionClosed(ConnectionError):
    """The peer closed the WebSocket or its underlying stream."""


def _bounded_timeout(value):
    value = float(value)
    if not math.isfinite(value) or not 0 < value <= MAX_TIMEOUT_SECONDS:
        raise ValueError("timeout must be greater than zero and at most 60 seconds")
    return value


class WebSocket:
    """RFC 6455 client with masked writes and bounded message assembly."""

    def __init__(self, stream, timeout=10.0):
        # Accepting an existing stream also permits deterministic offline tests.
        self._stream = stream
        self.timeout = _bounded_timeout(timeout)
        self._buffer = bytearray()
        self._fragment_opcode = None
        self._fragment_payload = bytearray()
        self._closed = False

    @classmethod
    def connect(cls, host, port, path="/api/websocket", timeout=10.0):
        """Connect over plain TCP; the caller should use HA's local endpoint."""
        timeout = _bounded_timeout(timeout)
        if not isinstance(host, str) or not host or any(c in host for c in "\r\n\x00"):
            raise ValueError("invalid host")
        if not isinstance(path, str) or not path.startswith("/") or any(
            c.isspace() or ord(c) < 32 or ord(c) > 126 for c in path
        ):
            raise ValueError("invalid HTTP path")
        if not isinstance(port, int) or isinstance(port, bool) or not 0 < port < 65536:
            raise ValueError("invalid port")
        stream = socket.create_connection((host, port), timeout=timeout)
        client = cls(stream, timeout)
        try:
            client._handshake(host, port, path)
            return client
        except BaseException:
            client._dispose()
            raise

    def _deadline(self, timeout=None):
        return time.monotonic() + _bounded_timeout(self.timeout if timeout is None else timeout)

    def _remaining(self, deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("WebSocket operation timed out")
        return remaining

    def _require_open(self):
        if self._closed:
            raise ConnectionClosed("WebSocket is closed")

    def _ensure(self, count, deadline):
        self._require_open()
        while len(self._buffer) < count:
            self._stream.settimeout(self._remaining(deadline))
            chunk = self._stream.recv(min(65536, count - len(self._buffer)))
            if not chunk:
                self._dispose()
                raise ConnectionClosed("WebSocket stream ended before the frame was complete")
            self._buffer.extend(chunk)

    def _handshake(self, host, port, path):
        deadline = self._deadline()
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        authority = "[{}]".format(host) if ":" in host and not host.startswith("[") else host
        request = (
            "GET {} HTTP/1.1\r\nHost: {}:{}\r\nUpgrade: websocket\r\n"
            "Connection: Upgrade\r\nSec-WebSocket-Key: {}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        ).format(path, authority, port, key).encode("ascii")
        self._stream.settimeout(self._remaining(deadline))
        self._stream.sendall(request)
        while b"\r\n\r\n" not in self._buffer:
            if len(self._buffer) >= MAX_HEADER_BYTES:
                raise ProtocolError("HTTP upgrade headers exceed the limit")
            self._stream.settimeout(self._remaining(deadline))
            chunk = self._stream.recv(min(4096, MAX_HEADER_BYTES - len(self._buffer)))
            if not chunk:
                raise ConnectionClosed("stream ended during HTTP upgrade")
            self._buffer.extend(chunk)
        boundary = self._buffer.index(b"\r\n\r\n") + 4
        if boundary > MAX_HEADER_BYTES:
            raise ProtocolError("HTTP upgrade headers exceed the limit")
        try:
            lines = bytes(self._buffer[:boundary - 4]).decode("ascii").split("\r\n")
        except UnicodeDecodeError as exc:
            raise ProtocolError("non-ASCII HTTP upgrade headers") from exc
        del self._buffer[:boundary]
        status = lines[0].split(" ", 2)
        if len(status) < 2 or status[:2] != ["HTTP/1.1", "101"]:
            raise ProtocolError("HTTP server refused the WebSocket upgrade")
        headers = {}
        for line in lines[1:]:
            if ":" not in line or line[:1].isspace():
                raise ProtocolError("malformed HTTP upgrade header")
            name, value = line.split(":", 1)
            if not name or any(c.isspace() for c in name):
                raise ProtocolError("malformed HTTP upgrade header name")
            headers.setdefault(name.lower(), []).append(value.strip())
        upgrades = ",".join(headers.get("upgrade", [])).lower().split(",")
        connections = ",".join(headers.get("connection", [])).lower().split(",")
        if "websocket" not in [v.strip() for v in upgrades] or "upgrade" not in [
            v.strip() for v in connections
        ]:
            raise ProtocolError("missing WebSocket upgrade headers")
        expected = base64.b64encode(hashlib.sha1((key + _GUID).encode("ascii")).digest()).decode("ascii")
        accepts = headers.get("sec-websocket-accept", [])
        if len(accepts) != 1 or not hmac.compare_digest(accepts[0], expected):
            raise ProtocolError("invalid Sec-WebSocket-Accept")
        if "sec-websocket-extensions" in headers or "sec-websocket-protocol" in headers:
            raise ProtocolError("server selected an unrequested extension or subprotocol")

    def fileno(self):
        self._require_open()
        return self._stream.fileno()

    def has_buffered_data(self):
        return bool(self._buffer)

    def wait_readable(self, timeout=None):
        """Wait for bytes; buffered data may contain only a partial frame."""
        self._require_open()
        timeout = _bounded_timeout(self.timeout if timeout is None else timeout)
        if self._buffer:
            return True
        return bool(select.select([self._stream], [], [], timeout)[0])

    def _send_frame(self, opcode, payload, deadline):
        self._require_open()
        size = len(payload)
        if size > MAX_MESSAGE_BYTES or (opcode >= 8 and size > 125):
            raise ValueError("outgoing WebSocket payload exceeds the limit")
        if size < 126:
            header = bytes((0x80 | opcode, 0x80 | size))
        elif size <= 65535:
            header = bytes((0x80 | opcode, 0xFE)) + struct.pack("!H", size)
        else:
            header = bytes((0x80 | opcode, 0xFF)) + struct.pack("!Q", size)
        mask = os.urandom(4)
        masked = bytes(value ^ mask[index & 3] for index, value in enumerate(payload))
        self._stream.settimeout(self._remaining(deadline))
        try:
            self._stream.sendall(header + mask + masked)
        except OSError:
            # sendall may have written a frame prefix. Retrying that stream would
            # desynchronize it, including when the error is a send timeout.
            self._dispose()
            raise

    def send_json(self, value, timeout=None):
        payload = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
        self._send_frame(1, payload, self._deadline(timeout))

    def _read_frame(self, deadline):
        # Nothing is consumed until a whole frame is available. A timeout may
        # therefore be retried without losing a header or a payload prefix.
        self._ensure(2, deadline)
        first, second = self._buffer[:2]
        final, opcode = bool(first & 0x80), first & 0x0F
        if first & 0x70 or opcode not in (0, 1, 2, 8, 9, 10):
            raise ProtocolError("unsupported WebSocket opcode or reserved bits")
        if second & 0x80:
            raise ProtocolError("server WebSocket frames must not be masked")
        size, offset = second & 0x7F, 2
        if size == 126:
            self._ensure(4, deadline)
            size, offset = struct.unpack("!H", self._buffer[2:4])[0], 4
            if size < 126:
                raise ProtocolError("non-minimal WebSocket payload length")
        elif size == 127:
            self._ensure(10, deadline)
            size, offset = struct.unpack("!Q", self._buffer[2:10])[0], 10
            if size <= 65535 or size & (1 << 63):
                raise ProtocolError("invalid WebSocket payload length")
        if opcode >= 8 and (not final or size > 125):
            raise ProtocolError("invalid control frame")
        if size > MAX_MESSAGE_BYTES:
            raise ProtocolError("incoming WebSocket payload exceeds the limit")
        if opcode < 8:
            if opcode == 0 and self._fragment_opcode is None:
                raise ProtocolError("continuation without a fragmented message")
            if opcode != 0 and self._fragment_opcode is not None:
                raise ProtocolError("new data frame interrupts a fragmented message")
            if len(self._fragment_payload) + size > MAX_MESSAGE_BYTES:
                raise ProtocolError("fragmented WebSocket message exceeds the limit")
        self._ensure(offset + size, deadline)
        payload = bytes(self._buffer[offset:offset + size])
        del self._buffer[:offset + size]
        return final, opcode, payload

    def recv_json(self, timeout=None):
        deadline = self._deadline(timeout)
        try:
            while True:
                final, opcode, payload = self._read_frame(deadline)
                if opcode == 9:
                    self._send_frame(10, payload, deadline)
                    continue
                if opcode == 10:
                    continue
                if opcode == 8:
                    self._receive_close(payload, deadline)
                if opcode in (1, 2):
                    self._fragment_opcode = opcode
                self._fragment_payload.extend(payload)
                if not final:
                    continue
                message_opcode = self._fragment_opcode
                message = bytes(self._fragment_payload)
                self._fragment_opcode = None
                self._fragment_payload.clear()
                if message_opcode != 1:
                    raise ProtocolError("JSON messages must use WebSocket text frames")
                try:
                    return json.loads(message.decode("utf-8"), parse_constant=_reject_json_constant)
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise ProtocolError("invalid UTF-8 JSON message") from exc
        except ProtocolError:
            self._dispose()
            raise
        except OSError as exc:
            # Socket timeouts preserve partial frames, other network errors do not.
            if not isinstance(exc, TimeoutError):
                self._dispose()
            raise

    def _receive_close(self, payload, deadline):
        if len(payload) == 1:
            raise ProtocolError("invalid close frame payload")
        if payload:
            code = struct.unpack("!H", payload[:2])[0]
            if code not in (1000, 1001, 1002, 1003, 1007, 1008, 1009, 1010, 1011, 1012, 1013, 1014) and not 3000 <= code <= 4999:
                raise ProtocolError("invalid WebSocket close code")
            try:
                payload[2:].decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ProtocolError("invalid UTF-8 close reason") from exc
        try:
            self._send_frame(8, payload, deadline)
        finally:
            self._dispose()
        raise ConnectionClosed("peer closed the WebSocket")

    def _dispose(self):
        if not self._closed:
            self._closed = True
            self._stream.close()
        self._buffer.clear()
        self._fragment_payload.clear()
        self._fragment_opcode = None

    def close(self):
        if self._closed:
            return
        try:
            self._send_frame(8, struct.pack("!H", 1000), self._deadline(min(1.0, self.timeout)))
        except OSError:
            pass
        finally:
            self._dispose()


def _reject_json_constant(_value):
    raise ProtocolError("non-finite JSON number")


def connect(host, port, path="/api/websocket", timeout=10.0):
    """Convenience alias for WebSocket.connect."""
    return WebSocket.connect(host, port, path, timeout)
