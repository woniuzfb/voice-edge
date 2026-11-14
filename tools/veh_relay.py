"""VEH relay: public HTTPS entrance ⇄ edge outbound WebSocket tunnel.

The relay is a transport tunnel only. It terminates public HTTP/1.1, keeps one outbound
WebSocket per edge, forwards whole HTTP requests as `http_request` frames and writes the
matching `http_response` frames back to the waiting connections. It does not understand
OAuth, MCP, JSON-RPC or tool calls, and it keeps no application state: the only state it
holds is the live WebSocket, the in-memory waiters and the `connection_id` / `http_id`
pair used to line a response up with its request.

Standard library only. A `.env` file in the current directory (or next to
this script) is loaded automatically; already-exported variables always win.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import logging
import os
import select
import socket
import ssl
import struct
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

LOG = logging.getLogger("veh_relay")


def load_env_file(dotenv_path: Optional[str] = None) -> Optional[Path]:
    """Load `KEY=VALUE` pairs from a `.env` file with the standard library only.

    Existing environment variables are never overwritten, so an `export` in the shell or
    a systemd/launchd unit beats the file. Candidate order: the current directory, then
    the directory holding this script. Returns the file that was loaded, if any.
    """
    if dotenv_path:
        candidates = [Path(dotenv_path)]
    else:
        candidates = [Path(".env"), Path(__file__).resolve().parent / ".env"]

    for cand in candidates:
        try:
            if not cand.is_file():
                continue
        except OSError:
            continue
        try:
            # `errors="replace"` plus a catch-all: one stray non-UTF-8 byte in a
            # hand-written .env must never abort module import (that kills the process
            # and only shows up downstream as a proxy 502).
            with open(cand, encoding="utf-8", errors="replace") as handle:
                for raw in handle:
                    line = raw.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, _, val = line.partition("=")
                    key = key.strip()
                    if key.startswith("export "):
                        key = key[len("export ") :].strip()
                    val = val.strip()
                    if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
                        val = val[1:-1]
                    if key and key not in os.environ:
                        os.environ[key] = val
        except Exception as err:
            LOG.warning("failed to read .env file at %s: %s", cand, err)
            continue
        return cand
    return None


# Loaded at import so every env read below (argparse defaults included) sees the file.
try:
    ENV_FILE_LOADED = load_env_file()
except Exception as _env_err:  # defensive: loading .env must never block startup
    LOG.warning("skipping .env load: %s", _env_err)
    ENV_FILE_LOADED = None

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
EDGE_PATH = "/v1/edge"

# Hop-by-hop headers never cross the tunnel; `Content-Length` is regenerated on both sides
# from the body actually written, so a stale length from one end can never truncate the other.
HOP_BY_HOP_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
        "content-length",
    }
)

PING_INTERVAL_SECONDS = 20.0
DEAD_AFTER_SECONDS = 60.0
WS_MAX_PAYLOAD = 8 * 1024 * 1024


def _strip_hop_headers(
    headers: Dict[str, str], connection_header: str = ""
) -> Dict[str, str]:
    """Drop hop-by-hop headers, including the ones named by `Connection`."""
    named = {
        token.strip().lower()
        for token in str(connection_header or "").split(",")
        if token.strip()
    }
    out: Dict[str, str] = {}
    for key, value in (headers or {}).items():
        low = str(key).strip().lower()
        if not low or low in HOP_BY_HOP_HEADERS or low in named:
            continue
        out[low] = str(value)
    return out


class _BodyFramingError(RuntimeError):
    """The request body could not be delimited, so the connection cannot be reused."""


class PendingHTTP:
    """One public HTTP request waiting for its `http_response` frame."""

    def __init__(self, http_id: str) -> None:
        self.http_id = http_id
        self.event = threading.Event()
        self.status = 0
        self.headers: Dict[str, str] = {}
        self.body = b""
        self.failed_reason = ""

    def deliver(self, status: int, headers: Dict[str, str], body: bytes) -> None:
        self.status = int(status)
        self.headers = dict(headers or {})
        self.body = body or b""
        self.event.set()

    def fail(self, reason: str) -> None:
        self.failed_reason = reason or "edge unavailable"
        self.event.set()

    @property
    def done(self) -> bool:
        return self.event.is_set()


class EdgeConnection:
    """The single live outbound WebSocket from an edge (this deployment is single tenant)."""

    def __init__(
        self, sock: socket.socket, connection_id: str, initial_buf: bytes = b""
    ) -> None:
        self.sock = sock
        self.connection_id = connection_id
        self.edge_id = ""
        self.protocol = 0
        self.alive = True
        self.last_seen = time.monotonic()
        self._send_lock = threading.Lock()
        self._pending: Dict[str, PendingHTTP] = {}
        self._pending_lock = threading.Lock()
        self._buf = bytes(initial_buf or b"")
        self._frag_opcode = 0
        self._frag_payload = b""

    # -- framing -----------------------------------------------------------
    def send_frame(self, opcode: int, payload: bytes) -> bool:
        header = bytearray()
        header.append(0x80 | (opcode & 0x0F))
        length = len(payload)
        if length < 126:
            header.append(length)
        elif length < 65536:
            header.append(126)
            header.extend(struct.pack(">H", length))
        else:
            header.append(127)
            header.extend(struct.pack(">Q", length))
        frame = bytes(header) + payload
        with self._send_lock:
            if not self.alive:
                return False
            try:
                self.sock.sendall(frame)
                return True
            except OSError as exc:
                LOG.info("edge send failed (%s): %s", self.connection_id, exc)
                self.alive = False
                return False

    def send_json(self, obj: Dict[str, Any]) -> bool:
        return self.send_frame(0x1, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def close(self, reason: str = "") -> None:
        if not self.alive:
            return
        self.alive = False
        try:
            self.sock.sendall(bytes([0x88, 0x00]))
        except OSError:
            pass
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass
        LOG.info("edge closed (%s) %s", self.connection_id, reason)

    # -- waiting HTTP requests --------------------------------------------
    def register(self, http_id: str) -> PendingHTTP:
        pending = PendingHTTP(http_id)
        with self._pending_lock:
            self._pending[http_id] = pending
        return pending

    def discard(self, http_id: str) -> None:
        with self._pending_lock:
            self._pending.pop(http_id, None)

    def fail_all_pending(self, reason: str) -> None:
        with self._pending_lock:
            pendings = list(self._pending.values())
            self._pending.clear()
        for pending in pendings:
            pending.fail(reason)

    # -- receive loop ------------------------------------------------------
    def read_loop(self) -> None:
        try:
            while self.alive:
                chunk = self.sock.recv(65536)
                if not chunk:
                    break
                self.last_seen = time.monotonic()
                self._buf += chunk
                if not self._drain_frames():
                    break
        except OSError as exc:
            LOG.info("edge read error (%s): %s", self.connection_id, exc)
        finally:
            self.close("read loop ended")
            RELAY.retire(self)

    def _drain_frames(self) -> bool:
        while True:
            frame = self._pop_frame()
            if frame is None:
                return True
            fin, opcode, payload = frame
            if not self._handle_frame(fin, opcode, payload):
                return False

    def _pop_frame(self) -> Optional[Tuple[bool, int, bytes]]:
        buf = self._buf
        if len(buf) < 2:
            return None
        b0, b1 = buf[0], buf[1]
        fin = bool(b0 & 0x80)
        opcode = b0 & 0x0F
        masked = bool(b1 & 0x80)
        length = b1 & 0x7F
        offset = 2
        if length == 126:
            if len(buf) < 4:
                return None
            length = struct.unpack(">H", buf[2:4])[0]
            offset = 4
        elif length == 127:
            if len(buf) < 10:
                return None
            length = struct.unpack(">Q", buf[2:10])[0]
            offset = 10
        if length > WS_MAX_PAYLOAD:
            LOG.warning("edge frame too large (%d bytes)", length)
            self.alive = False
            return None
        mask_key = b""
        if masked:
            if len(buf) < offset + 4:
                return None
            mask_key = buf[offset : offset + 4]
            offset += 4
        if len(buf) < offset + length:
            return None
        payload = buf[offset : offset + length]
        if masked and mask_key:
            payload = bytes(payload[i] ^ mask_key[i % 4] for i in range(len(payload)))
        self._buf = buf[offset + length :]
        return fin, opcode, payload

    def _handle_frame(self, fin: bool, opcode: int, payload: bytes) -> bool:
        if opcode == 0x8:  # close
            return False
        if opcode == 0x9:  # ping
            return self.send_frame(0xA, payload)
        if opcode == 0xA:  # pong
            return True
        if opcode in (0x1, 0x2):
            if fin:
                self._handle_message(opcode, payload)
                return True
            self._frag_opcode = opcode
            self._frag_payload = payload
            return True
        if opcode == 0x0:  # continuation
            self._frag_payload += payload
            if fin:
                self._handle_message(self._frag_opcode, self._frag_payload)
                self._frag_opcode = 0
                self._frag_payload = b""
            return True
        LOG.debug("ignoring ws opcode %d", opcode)
        return True

    def _handle_message(self, opcode: int, payload: bytes) -> None:
        if opcode != 0x1:
            return
        try:
            msg = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            LOG.warning("edge sent a non-JSON text frame")
            return
        if not isinstance(msg, dict):
            return
        kind = str(msg.get("type") or "")
        if kind == "hello":
            self.edge_id = str(msg.get("edge_id") or "")
            try:
                self.protocol = int(msg.get("protocol") or 0)
            except (TypeError, ValueError):
                self.protocol = 0
            LOG.info(
                "edge hello (%s) edge_id=%s protocol=%s",
                self.connection_id,
                self.edge_id,
                self.protocol,
            )
            return
        if kind == "http_response":
            if str(msg.get("connection_id") or "") != self.connection_id:
                # A response from an older connection is stale by definition.
                LOG.info("dropping response from stale connection_id")
                return
            http_id = str(msg.get("http_id") or "")
            with self._pending_lock:
                pending = self._pending.pop(http_id, None)
            if pending is None:
                return
            raw_headers = msg.get("headers")
            headers = (
                {
                    str(k).lower(): str(v)
                    for k, v in raw_headers.items()
                    if isinstance(raw_headers, dict)
                }
                if isinstance(raw_headers, dict)
                else {}
            )
            body = b""
            body_b64 = msg.get("body_b64")
            if isinstance(body_b64, str) and body_b64:
                try:
                    body = base64.b64decode(body_b64)
                except (ValueError, TypeError):
                    body = b""
            try:
                status = int(msg.get("status") or 0)
            except (TypeError, ValueError):
                status = 0
            pending.deliver(status, headers, body)
            return
        LOG.debug("ignoring edge message type %r", kind)


class RelayState:
    """Process-wide relay state: the live edge connection and the `http_id` counter."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._edge: Optional[EdgeConnection] = None
        self._http_seq = 0
        self._conn_seq = 0
        self.started = time.time()

    def adopt(self, sock: socket.socket, initial_buf: bytes = b"") -> EdgeConnection:
        with self._lock:
            previous = self._edge
            self._conn_seq += 1
            connection_id = "ws_%08x" % self._conn_seq
            edge = EdgeConnection(sock, connection_id, initial_buf)
            self._edge = edge
        if previous is not None:
            previous.fail_all_pending("edge replaced by a newer connection")
            previous.close("replaced")
        LOG.info("adopting edge connection %s", connection_id)
        return edge

    def retire(self, edge: EdgeConnection) -> None:
        with self._lock:
            if self._edge is edge:
                self._edge = None
        edge.fail_all_pending("edge connection closed")

    def current(self) -> Optional[EdgeConnection]:
        with self._lock:
            edge = self._edge
        if edge is not None and not edge.alive:
            return None
        return edge

    def next_http_id(self) -> str:
        with self._lock:
            self._http_seq += 1
            return "h_%08x" % self._http_seq


RELAY = RelayState()
RELAY_TOKEN = ""


def _ws_accept(key: str) -> str:
    digest = hashlib.sha1((key + WS_GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


def _drain_buffered_request_bytes(sock: socket.socket, rfile: Any) -> bytes:
    """Take back any WebSocket bytes the HTTP header parser buffered past the request head.

    `BaseHTTPRequestHandler` reads headers through a buffered file object, so the first
    client frame can already sit in that buffer when the upgrade completes. Those bytes
    belong to the frame loop, not to HTTP.
    """
    try:
        sock.setblocking(False)
    except OSError:
        return b""
    try:
        return rfile.read1(65536) or b""
    except (BlockingIOError, OSError, ValueError):
        return b""
    finally:
        try:
            sock.setblocking(True)
        except OSError:
            pass


def _client_gone(sock: socket.socket) -> bool:
    """Best-effort half-close probe: only a clean EOF counts as a disconnect.

    TLS sockets cannot be peeked and report it as `ValueError`; treating that as "still
    here" keeps the request waiting instead of guessing.
    """
    if isinstance(sock, ssl.SSLSocket):
        return False
    try:
        readable, _, _ = select.select([sock], [], [], 0)
    except (OSError, ValueError):
        return True
    if not readable:
        return False
    try:
        data = sock.recv(1, socket.MSG_PEEK)
    except (BlockingIOError, ValueError):
        return False
    except OSError:
        return True
    return not data


class RelayHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "VEHRelay"

    def log_message(self, fmt: str, *args: Any) -> None:
        LOG.debug("%s - %s", self.address_string(), fmt % args)

    # -- dispatch ----------------------------------------------------------
    def do_GET(self) -> None:
        if urlsplit(self.path).path == EDGE_PATH:
            self._handle_edge_upgrade()
            return
        self._forward_http()

    def do_POST(self) -> None:
        self._forward_http()

    def do_PUT(self) -> None:
        self._forward_http()

    def do_PATCH(self) -> None:
        self._forward_http()

    def do_DELETE(self) -> None:
        self._forward_http()

    def do_HEAD(self) -> None:
        self._forward_http()

    def do_OPTIONS(self) -> None:
        self._forward_http()

    # -- edge upgrade ------------------------------------------------------
    def _handle_edge_upgrade(self) -> None:
        upgrade = str(self.headers.get("Upgrade") or "").lower()
        if upgrade != "websocket":
            self._send_plain(400, b"websocket upgrade required\n")
            return
        auth = str(self.headers.get("Authorization") or "")
        token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        if not RELAY_TOKEN or token != RELAY_TOKEN:
            LOG.warning("rejecting edge connection: bad transport token")
            self._send_plain(403, b"forbidden\n")
            return
        key = str(self.headers.get("Sec-WebSocket-Key") or "").strip()
        if not key:
            self._send_plain(400, b"missing Sec-WebSocket-Key\n")
            return
        handshake = (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {_ws_accept(key)}\r\n\r\n"
        )
        sock = self.connection
        try:
            sock.sendall(handshake.encode("ascii"))
        except OSError:
            return
        self.close_connection = True
        leftover = _drain_buffered_request_bytes(sock, self.rfile)
        edge = RELAY.adopt(sock, leftover)
        edge.read_loop()

    # -- public HTTP forwarding -------------------------------------------
    def _read_chunked_body(self) -> bytes:
        """Decode a `Transfer-Encoding: chunked` body into one contiguous buffer.

        Reverse proxies in front of the relay commonly re-frame uploads as chunked, so a
        `Content-Length`-only reader would silently forward an empty body and leave the
        rest of the upload in the socket, where the next keep-alive `readline()` picks it
        up and answers `400 Bad request syntax`.
        """
        body = bytearray()
        while True:
            size_line = self.rfile.readline(65536)
            if not size_line:
                raise _BodyFramingError("chunked body ended before the last chunk")
            size_text = size_line.split(b";", 1)[0].strip()
            try:
                size = int(size_text, 16)
            except ValueError as exc:
                raise _BodyFramingError(f"bad chunk size {size_text!r}") from exc
            if size <= 0:
                while True:
                    trailer = self.rfile.readline(65536)
                    if trailer in (b"\r\n", b"\n", b""):
                        break
                return bytes(body)
            remaining = size
            while remaining > 0:
                chunk = self.rfile.read(remaining)
                if not chunk:
                    raise _BodyFramingError("chunked body truncated")
                body.extend(chunk)
                remaining -= len(chunk)
            self.rfile.read(2)  # CRLF that closes this chunk

    def _read_request_body(self) -> bytes:
        transfer_encoding = str(self.headers.get("Transfer-Encoding") or "").lower()
        if "chunked" in transfer_encoding:
            return self._read_chunked_body()
        raw_len = str(self.headers.get("Content-Length") or "").strip()
        if not raw_len:
            return b""
        try:
            length = int(raw_len)
        except (TypeError, ValueError) as exc:
            raise _BodyFramingError(f"bad Content-Length {raw_len!r}") from exc
        if length <= 0:
            return b""
        body = self.rfile.read(length)
        if len(body) != length:
            raise _BodyFramingError("request body truncated")
        return body

    def _forward_http(self) -> None:
        try:
            body = self._read_request_body()
        except _BodyFramingError as exc:
            # The stream is no longer framed, so it cannot be reused for another request.
            LOG.warning(
                "refusing malformed request body from %s: %s", self.client_address, exc
            )
            self.close_connection = True
            self._send_plain(400, b"bad request body\n")
            return
        edge = RELAY.current()
        if edge is None:
            self._send_plain(502, b"edge unavailable\n")
            return
        parts = urlsplit(self.path)
        headers = _strip_hop_headers(
            {k: v for k, v in self.headers.items()},
            str(self.headers.get("Connection") or ""),
        )
        http_id = RELAY.next_http_id()
        pending = edge.register(http_id)
        frame = {
            "type": "http_request",
            "connection_id": edge.connection_id,
            "http_id": http_id,
            "method": self.command,
            "path": parts.path or "/",
            "query": parts.query,
            "headers": headers,
            "body_b64": base64.b64encode(body).decode("ascii") if body else "",
        }
        if not edge.send_json(frame):
            edge.discard(http_id)
            self._send_plain(502, b"edge unavailable\n")
            return
        try:
            self._wait_and_reply(edge, pending)
        finally:
            edge.discard(http_id)
            if not pending.done:
                edge.send_json(
                    {
                        "type": "http_abort",
                        "connection_id": edge.connection_id,
                        "http_id": http_id,
                    }
                )

    def _wait_and_reply(self, edge: EdgeConnection, pending: PendingHTTP) -> None:
        client_gone = False
        while not pending.done:
            pending.event.wait(0.5)
            if pending.done:
                break
            if not edge.alive:
                break
            if _client_gone(self.connection):
                client_gone = True
                break
        if not pending.done:
            self._send_plain(502, b"edge unavailable\n")
            return
        if client_gone:
            return
        if pending.failed_reason:
            self._send_plain(502, b"edge unavailable\n")
            return
        self._write_response(
            pending.status,
            pending.headers,
            pending.body,
            head_only=self.command == "HEAD",
        )

    def _write_response(
        self, status: int, headers: Dict[str, str], body: bytes, *, head_only: bool
    ) -> None:
        try:
            self.send_response(int(status) if status else 502)
            conn_hdr = ""
            for k, v in (headers or {}).items():
                if str(k).strip().lower() == "connection":
                    conn_hdr = str(v)
                    break
            cleaned = _strip_hop_headers(headers or {}, conn_hdr)
            for key, value in cleaned.items():
                self.send_header(str(key), str(value))
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if body and not head_only:
                self.wfile.write(body)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError) as exc:
            LOG.info("public client went away before the response: %s", exc)
            self.close_connection = True

    def _send_plain(self, status: int, payload: bytes) -> None:
        self._write_response(
            status,
            {"content-type": "text/plain; charset=utf-8", "cache-control": "no-store"},
            payload,
            head_only=self.command == "HEAD",
        )


class RelayHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr: Tuple[str, int], handler: Any, tls: ssl.SSLContext | None):
        self.tls_context = tls
        super().__init__(addr, handler)

    def get_request(self) -> Tuple[socket.socket, Any]:
        sock, addr = super().get_request()
        if self.tls_context is not None:
            sock = self.tls_context.wrap_socket(sock, server_side=True)
        sock.settimeout(None)
        return sock, addr

    def handle_error(self, request: Any, client_address: Any) -> None:
        exc = sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionResetError, ssl.SSLError)):
            return
        LOG.warning("request error from %s: %s", client_address, exc)


def _ping_loop() -> None:
    while True:
        time.sleep(PING_INTERVAL_SECONDS)
        edge = RELAY.current()
        if edge is None:
            continue
        if time.monotonic() - edge.last_seen > DEAD_AFTER_SECONDS:
            LOG.warning("edge silent for %.0fs; declaring it dead", DEAD_AFTER_SECONDS)
            edge.close("heartbeat timeout")
            RELAY.retire(edge)
            continue
        edge.send_frame(0x9, b"veh")


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="VEH relay (public HTTPS to edge WSS)")
    parser.add_argument("--host", default=os.environ.get("VE_RELAY_HOST", "0.0.0.0"))
    parser.add_argument(
        "--port", type=int, default=int(os.environ.get("VE_RELAY_PORT", "443"))
    )
    parser.add_argument("--cert", default=os.environ.get("VE_RELAY_CERT", ""))
    parser.add_argument("--key", default=os.environ.get("VE_RELAY_KEY", ""))
    parser.add_argument(
        "--no-tls",
        action="store_true",
        help="serve plain HTTP (local connectivity tests only)",
    )
    parser.add_argument(
        "--log-level", default=os.environ.get("VE_RELAY_LOG_LEVEL", "INFO")
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    if ENV_FILE_LOADED is not None:
        LOG.info("loaded environment from %s", ENV_FILE_LOADED)

    global RELAY_TOKEN
    RELAY_TOKEN = str(os.environ.get("VE_USER_TOOL_RELAY_TOKEN") or "").strip()
    if not RELAY_TOKEN:
        LOG.error("VE_USER_TOOL_RELAY_TOKEN must be set; refusing to start")
        return 2

    tls: ssl.SSLContext | None = None
    if not args.no_tls:
        if not args.cert or not args.key:
            LOG.error("--cert and --key are required unless --no-tls is given")
            return 2
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.load_cert_chain(args.cert, args.key)
        tls.minimum_version = ssl.TLSVersion.TLSv1_2

    server = RelayHTTPServer((args.host, args.port), RelayHandler, tls)
    threading.Thread(target=_ping_loop, daemon=True, name="veh-relay-ping").start()
    scheme = "https" if tls is not None else "http"
    LOG.info("VEH relay listening on %s://%s:%d", scheme, args.host, args.port)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        LOG.info("relay interrupted; shutting down")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
