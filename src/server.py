"""
TCP Simulator - WebSocket + HTTP Server (pure stdlib, no external deps)
=======================================================================
- HTTP server serves the frontend HTML on port 8080
- WebSocket server on port 8081 streams simulation events to the browser
"""

import asyncio
import hashlib
import base64
import json
import struct
import socket
import threading
import http.server
import os
import sys
from simulation import SimulationOrchestrator

# ─── WebSocket frame helpers ──────────────────────────────────────────────────

def _ws_handshake(conn: socket.socket, request_data: bytes) -> bool:
    """Parse HTTP Upgrade request and send 101 Switching Protocols."""
    lines = request_data.decode(errors="replace").split("\r\n")
    headers = {}
    for line in lines[1:]:
        if ": " in line:
            k, v = line.split(": ", 1)
            headers[k.lower()] = v

    key = headers.get("sec-websocket-key", "")
    if not key:
        return False

    magic   = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
    accept  = base64.b64encode(
        hashlib.sha1((key + magic).encode()).digest()
    ).decode()

    response = (
        "HTTP/1.1 101 Switching Protocols\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Accept: {accept}\r\n"
        "\r\n"
    )
    conn.sendall(response.encode())
    return True


def _ws_send(conn: socket.socket, message: str):
    """Send a text WebSocket frame."""
    data    = message.encode("utf-8")
    length  = len(data)
    header  = bytearray([0x81])   # FIN + text opcode

    if length <= 125:
        header.append(length)
    elif length <= 65535:
        header.append(126)
        header += struct.pack("!H", length)
    else:
        header.append(127)
        header += struct.pack("!Q", length)

    conn.sendall(bytes(header) + data)


def _ws_recv(conn: socket.socket) -> str | None:
    """Receive one WebSocket text frame (blocking)."""
    try:
        header = _recv_exact(conn, 2)
        if not header:
            return None

        opcode  = header[0] & 0x0F
        masked  = (header[1] & 0x80) != 0
        length  = header[1] & 0x7F

        if opcode == 0x8:   # close frame
            return None

        if length == 126:
            length = struct.unpack("!H", _recv_exact(conn, 2))[0]
        elif length == 127:
            length = struct.unpack("!Q", _recv_exact(conn, 8))[0]

        mask_key = _recv_exact(conn, 4) if masked else b"\x00" * 4
        payload  = bytearray(_recv_exact(conn, length))

        for i in range(length):
            payload[i] ^= mask_key[i % 4]

        return payload.decode("utf-8")
    except Exception:
        return None


def _recv_exact(conn: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = conn.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("Socket closed")
        buf += chunk
    return buf


# ─── Client handler ───────────────────────────────────────────────────────────

class SimClient:
    def __init__(self, conn: socket.socket, addr):
        self.conn       = conn
        self.addr       = addr
        self._lock      = threading.Lock()
        self._sim       = None
        self._sim_thread= None

    def run(self):
        # Read HTTP request for handshake
        raw = b""
        self.conn.settimeout(5)
        try:
            while b"\r\n\r\n" not in raw:
                raw += self.conn.recv(1024)
        except socket.timeout:
            self.conn.close()
            return

        if not _ws_handshake(self.conn, raw):
            self.conn.close()
            return

        self.conn.settimeout(None)
        self._send({"type": "connected", "msg": "WebSocket connected"})

        try:
            while True:
                msg = _ws_recv(self.conn)
                if msg is None:
                    break
                try:
                    cmd = json.loads(msg)
                    self._handle(cmd)
                except Exception as e:
                    self._send({"type": "error", "msg": str(e)})
        finally:
            # Runs on both clean close AND abrupt browser disconnect/refresh.
            # Stops the orchestrator (signals all threads to exit) then joins
            # the sim thread so we don't leave zombie threads eating CPU/ports.
            self._teardown()

    def _teardown(self):
        if self._sim:
            self._sim.stop()          # signals Sender, Receiver, Channel to exit
        if self._sim_thread and self._sim_thread.is_alive():
            self._sim_thread.join(timeout=3)   # wait up to 3s for clean exit
        try:
            self.conn.close()
        except Exception:
            pass

    def _handle(self, cmd: dict):
        action = cmd.get("action")

        if action == "start":
            if self._sim and self._sim._running:
                self._send({"type": "error", "msg": "Simulation already running"})
                return
            params = cmd.get("params", {})
            self._start_simulation(
                total_packets  = int(params.get("total_packets", 10)),
                initial_window = int(params.get("initial_window", 4)),
                loss_prob      = float(params.get("loss_prob", 0.2)),
            )

        elif action == "stop":
            if self._sim:
                self._sim.stop()
                self._send({"type": "stopped", "msg": "Simulation stopped"})

    def _start_simulation(self, total_packets, initial_window, loss_prob):
        def log_cb(msg):
            self._send({"type": "log", "msg": msg})

        def stats_cb(entry):
            self._send({"type": "stats", "data": entry})

        def done_cb():
            self._send({"type": "done", "msg": "Simulation complete"})

        self._sim = SimulationOrchestrator(
            total_packets  = total_packets,
            initial_window = initial_window,
            loss_prob      = loss_prob,
            log_cb         = log_cb,
            stats_cb       = stats_cb,
        )

        def _run():
            self._sim.run()
            done_cb()

        self._sim_thread = threading.Thread(target=_run, daemon=True)
        self._sim_thread.start()
        self._send({"type": "started", "msg": f"Simulation started: {total_packets} packets, loss={loss_prob}"})

    def _send(self, obj: dict):
        try:
            with self._lock:
                _ws_send(self.conn, json.dumps(obj))
        except Exception:
            pass


# ─── WebSocket server ─────────────────────────────────────────────────────────

class WSServer(threading.Thread):
    def __init__(self, host="127.0.0.1", port=8081):
        super().__init__(daemon=True)
        self.host = host
        self.port = port

    def run(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((self.host, self.port))
        srv.listen(5)
        print(f"[WS ] Listening on ws://{self.host}:{self.port}")
        while True:
            conn, addr = srv.accept()
            client = SimClient(conn, addr)
            threading.Thread(target=client.run, daemon=True).start()


# ─── HTTP server ──────────────────────────────────────────────────────────────

class HTMLHandler(http.server.SimpleHTTPRequestHandler):
    """Serves only the frontend HTML file."""

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            # Project layout:
            #  - repo root
            #     - src/server.py
            #     - web/index.html
            root = os.path.dirname(os.path.dirname(__file__))
            html_path = os.path.join(root, "web", "index.html")

            # Fallback for older layout where index.html lived next to server.py.
            if not os.path.exists(html_path):
                html_path = os.path.join(os.path.dirname(__file__), "index.html")

            try:
                with open(html_path, "rb") as f:
                    content = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)
            except FileNotFoundError:
                self.send_error(404, "index.html not found")
        else:
            self.send_error(404)

    def log_message(self, *args):
        pass  # suppress default logging


def start_http(host="127.0.0.1", port=8080):
    httpd = http.server.HTTPServer((host, port), HTMLHandler)
    print(f"[HTTP] Listening on http://{host}:{port}")
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd


# ─── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    start_http()
    ws = WSServer()
    ws.start()

    print("\n  TCP Sliding Window Simulator")
    print("  ─────────────────────────────────────────────")
    print("  Open your browser → http://127.0.0.1:8080")
    print("  Press Ctrl+C to stop.\n")

    try:
        threading.Event().wait()   # block forever
    except KeyboardInterrupt:
        print("\nShutting down.")
        sys.exit(0)
