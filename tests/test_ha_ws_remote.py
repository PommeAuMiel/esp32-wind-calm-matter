"""Offline stream tests for the stdlib HA JSON WebSocket transport."""
import base64
import hashlib
import importlib.util
from pathlib import Path
import struct
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location(
    "ha_ws_remote", Path(__file__).resolve().parents[1] / "tools/ha_ws_remote.py")
ws = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ws)


def frame(payload, opcode=1, final=True):
    size = len(payload)
    first = (0x80 if final else 0) | opcode
    if size < 126:
        return bytes((first, size)) + payload
    if size <= 65535:
        return bytes((first, 126)) + struct.pack("!H", size) + payload
    return bytes((first, 127)) + struct.pack("!Q", size) + payload


def decode_client_frame(raw):
    first, second = raw[:2]
    if not second & 0x80:
        raise AssertionError("client frame was not masked")
    size, offset = second & 0x7f, 2
    if size == 126:
        size, offset = struct.unpack("!H", raw[2:4])[0], 4
    elif size == 127:
        size, offset = struct.unpack("!Q", raw[2:10])[0], 10
    mask = raw[offset:offset + 4]
    body = raw[offset + 4:]
    if len(body) != size:
        raise AssertionError("incorrect encoded frame length")
    return first & 0x0f, bytes(value ^ mask[index & 3] for index, value in enumerate(body))


class FakeStream:
    def __init__(self, chunks=(), handshake=False, bad_accept=False, extras=b""):
        self.chunks = list(chunks)
        self.sent = []
        self.timeouts = []
        self.closed = False
        self.handshake = handshake
        self.bad_accept = bad_accept
        self.extras = extras
        self.received = 0

    def settimeout(self, timeout):
        self.timeouts.append(timeout)

    def sendall(self, data):
        self.sent.append(data)
        if self.handshake and data.startswith(b"GET "):
            key = data.split(b"Sec-WebSocket-Key: ", 1)[1].split(b"\r\n", 1)[0]
            accept = base64.b64encode(hashlib.sha1(key + ws._GUID.encode("ascii")).digest())
            if self.bad_accept:
                accept = b"incorrect"
            self.chunks.insert(0, b"HTTP/1.1 101 Switching Protocols\r\n"
                               b"Upgrade: websocket\r\nConnection: keep-alive, Upgrade\r\n"
                               b"Sec-WebSocket-Accept: " + accept + b"\r\n" + self.extras + b"\r\n")

    def recv(self, count):
        if not self.chunks:
            return b""
        chunk = self.chunks[0]
        if isinstance(chunk, BaseException):
            self.chunks.pop(0)
            raise chunk
        result = chunk[:count]
        if len(result) == len(chunk):
            self.chunks.pop(0)
        else:
            self.chunks[0] = chunk[count:]
        self.received += len(result)
        return result

    def close(self):
        self.closed = True

    def fileno(self):
        return 123


class WebSocketTransportTests(unittest.TestCase):
    def test_handshake_validates_accept_and_keeps_pipelined_message(self):
        stream = FakeStream([frame(b'{"type":"auth_required"}')], handshake=True)
        with patch.object(ws.socket, "create_connection", return_value=stream) as connect:
            client = ws.WebSocket.connect("127.0.0.1", 8123, "/api/websocket", timeout=3)
        connect.assert_called_once_with(("127.0.0.1", 8123), timeout=3.0)
        self.assertEqual(client.recv_json(), {"type": "auth_required"})
        self.assertTrue(all(0 < value <= 3 for value in stream.timeouts))
        self.assertIn(b"Sec-WebSocket-Version: 13\r\n", stream.sent[0])

    def test_bad_accept_and_unsolicited_extensions_close_stream(self):
        for stream in (FakeStream(handshake=True, bad_accept=True),
                       FakeStream(handshake=True, extras=b"Sec-WebSocket-Extensions: permessage-deflate\r\n")):
            with self.subTest(stream=stream), patch.object(ws.socket, "create_connection", return_value=stream):
                with self.assertRaises(ws.ProtocolError):
                    ws.connect("localhost", 8123)
                self.assertTrue(stream.closed)

    def test_fragmented_json_with_interleaved_ping_and_pong(self):
        incoming = frame(b'{"name":"', final=False) + frame(b"hello", opcode=9)
        incoming += frame(b"ignored", opcode=10) + frame("é".encode("utf-8"), opcode=0, final=False)
        incoming += frame(b'"}', opcode=0)
        stream = FakeStream([incoming[index:index + 1] for index in range(len(incoming))])
        client = ws.WebSocket(stream)
        self.assertEqual(client.recv_json(), {"name": "é"})
        self.assertEqual(decode_client_frame(stream.sent[0]), (10, b"hello"))

    def test_timeout_can_resume_inside_header_payload_and_fragment(self):
        pieces = (frame(b'{"ok":', final=False), frame(b"true}", opcode=0))
        stream = FakeStream([pieces[0][:1], TimeoutError(), pieces[0][1:-2], TimeoutError(),
                             pieces[0][-2:], pieces[1][:3], TimeoutError(), pieces[1][3:]])
        client = ws.WebSocket(stream)
        for _ in range(3):
            with self.assertRaises(TimeoutError):
                client.recv_json()
            self.assertFalse(stream.closed)
        self.assertEqual(client.recv_json(), {"ok": True})

    def test_truncated_frame_and_fragment_are_reported_and_closed(self):
        for payload in (b"\x81", frame(b'{"ok":true}')[:-1], frame(b'{', final=False)):
            with self.subTest(payload=payload):
                stream = FakeStream([payload])
                client = ws.WebSocket(stream)
                with self.assertRaises(ws.ConnectionClosed):
                    client.recv_json()
                self.assertTrue(stream.closed)

    def test_single_frame_and_fragmented_message_sizes_are_bounded(self):
        self.assertEqual(ws.MAX_MESSAGE_BYTES, 4 * 1024 * 1024)
        # The oversized header must be rejected without allocating its payload.
        stream = FakeStream([b"\x81\x7f" + struct.pack("!Q", ws.MAX_MESSAGE_BYTES + 1)])
        with self.assertRaises(ws.ProtocolError):
            ws.WebSocket(stream).recv_json()
        self.assertEqual(stream.received, 10)
        self.assertTrue(stream.closed)
        with patch.object(ws, "MAX_MESSAGE_BYTES", 32):
            stream = FakeStream([frame(b"x" * 24, final=False), frame(b"x" * 9, opcode=0)])
            with self.assertRaises(ws.ProtocolError):
                ws.WebSocket(stream).recv_json()
            self.assertEqual(stream.received, 28)  # First payload plus the second header.
            self.assertTrue(stream.closed)

    def test_invalid_frame_shapes_are_rejected(self):
        invalid = (b"\xc1\x00", b"\x81\x80", b"\x83\x00", b"\x00\x00", b"\x09\x00",
                   b"\x89\x7e\x00\x7e", b"\x81\x7e\x00\x01", b"\x81\x7f" + struct.pack("!Q", 126),
                   b"\x81\x7f" + struct.pack("!Q", 1 << 63),
                   frame(b"{", final=False) + frame(b"{}"))
        for payload in invalid:
            with self.subTest(payload=payload):
                stream = FakeStream([payload])
                with self.assertRaises(ws.ProtocolError):
                    ws.WebSocket(stream).recv_json()
                self.assertTrue(stream.closed)

    def test_json_must_be_valid_utf8_text_and_finite(self):
        for payload in (frame(b"{}", opcode=2), frame(b"\xff"), frame(b"not json"), frame(b'{"n":NaN}')):
            with self.subTest(payload=payload):
                stream = FakeStream([payload])
                with self.assertRaises(ws.ProtocolError):
                    ws.WebSocket(stream).recv_json()
                self.assertTrue(stream.closed)

    def test_writes_are_masked_with_short_and_extended_lengths(self):
        for length in (1, 130, 66000):
            stream = FakeStream()
            client = ws.WebSocket(stream)
            client.send_json({"text": "é" * length})
            opcode, payload = decode_client_frame(stream.sent[0])
            self.assertEqual(opcode, 1)
            self.assertEqual(ws.json.loads(payload.decode("utf-8")), {"text": "é" * length})

    def test_send_timeout_disposes_a_potentially_partial_frame(self):
        stream = FakeStream()
        client = ws.WebSocket(stream)
        with patch.object(stream, "sendall", side_effect=TimeoutError("partial write")):
            with self.assertRaises(TimeoutError):
                client.send_json({"id": 1})
        self.assertTrue(stream.closed)
        with self.assertRaises(ws.ConnectionClosed):
            client.send_json({"id": 2})

    def test_close_handshake_echoes_and_disposes(self):
        payload = struct.pack("!H", 1000) + b"done"
        stream = FakeStream([frame(payload, opcode=8)])
        client = ws.WebSocket(stream)
        with self.assertRaises(ws.ConnectionClosed):
            client.recv_json()
        self.assertEqual(decode_client_frame(stream.sent[0]), (8, payload))
        self.assertTrue(stream.closed)
        client.close()
        self.assertEqual(len(stream.sent), 1)
        with self.assertRaises(ws.ConnectionClosed):
            client.fileno()

    def test_bad_close_payload_is_rejected(self):
        for payload in (b"x", struct.pack("!H", 1005), struct.pack("!H", 1000) + b"\xff"):
            stream = FakeStream([frame(payload, opcode=8)])
            with self.assertRaises(ws.ProtocolError):
                ws.WebSocket(stream).recv_json()
            self.assertTrue(stream.closed)

    def test_wait_and_parameters_are_bounded(self):
        stream = FakeStream()
        client = ws.WebSocket(stream, timeout=2)
        with patch.object(ws.select, "select", return_value=([stream], [], [])) as select_call:
            self.assertTrue(client.wait_readable())
        select_call.assert_called_once_with([stream], [], [], 2.0)
        client._buffer.extend(b"x")
        with patch.object(ws.select, "select") as select_call:
            self.assertTrue(client.wait_readable())
        select_call.assert_not_called()
        for timeout in (0, -1, 61, float("inf"), float("nan")):
            with self.assertRaises(ValueError):
                ws.WebSocket(stream, timeout)
        with patch.object(ws.socket, "create_connection") as network:
            for host, port, path in (("x\r\n", 8123, "/api/websocket"), ("x", 0, "/"), ("x", 8123, "/bad path")):
                with self.assertRaises(ValueError):
                    ws.WebSocket.connect(host, port, path)
            network.assert_not_called()


if __name__ == "__main__":
    unittest.main()
