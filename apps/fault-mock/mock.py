"""Mock of a system that lives outside the cluster (VM, appliance, database), with faults
that can be changed at runtime.

MODE=http       Small JSON HTTP(S) service (core banking, card switch, SMS gateway).
MODE=tcp-proxy  TCP proxy in front of a real server (PostgreSQL standing in for Oracle).

Admin API (always plain HTTP on ADMIN_PORT, default 9000):
  GET  /fault            current fault state
  POST /fault            merge JSON: {"latency_ms": 2500, "jitter_ms": 1500,
                                      "error_rate": 0.3, "mode": "up|down|unreachable"}
  POST /reset            back to healthy

mode=down         listener closed: clients get "connection refused" at once (Envoy flag UF)
mode=unreachable  connections are accepted but nothing is ever answered, like a firewall
                  dropping traffic: clients hit their timeout (Envoy flag UT)
"""
import json
import os
import random
import select
import signal
import socket
import ssl
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from socketserver import ThreadingTCPServer, BaseRequestHandler

NAME = os.environ.get("MOCK_NAME", "mock")
MODE = os.environ.get("MODE", "http")
PORT = int(os.environ.get("PORT", "8080"))
ADMIN_PORT = int(os.environ.get("ADMIN_PORT", "9000"))
BASE_LATENCY = [float(x) for x in os.environ.get("BASE_LATENCY_MS", "20,60").split(",")]
TLS_CERT, TLS_KEY = os.environ.get("TLS_CERT"), os.environ.get("TLS_KEY")
UPSTREAM = os.environ.get("UPSTREAM", "")  # host:port for tcp-proxy

HEALTHY = {"latency_ms": 0, "jitter_ms": 0, "error_rate": 0.0, "mode": "up"}
fault = dict(HEALTHY)
lock = threading.Lock()


def log(msg: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} {NAME} {msg}", flush=True)


def current() -> dict:
    with lock:
        return dict(fault)


def injected_delay(f: dict) -> float:
    base = random.uniform(*BASE_LATENCY) if len(BASE_LATENCY) == 2 else BASE_LATENCY[0]
    extra = f["latency_ms"] + random.uniform(-f["jitter_ms"], f["jitter_ms"]) if f["latency_ms"] else 0
    return max(0.0, base + extra) / 1000


# ------------------------------------------------------------------ HTTP mock
RESPONSES = {
    "core-banking": lambda: (200, {"status": "booked", "ledger_ref": f"LDG{random.randint(10**7, 10**8 - 1)}"}),
    "card-switch": lambda: (200, {"status": "approved", "auth_code": f"{random.randint(0, 999999):06d}"}),
    "sms-gateway": lambda: (202, {"status": "queued"}),
    "credit-bureau": lambda: (200, {"status": "ok", "score": random.randint(300, 850)}),
}


class MockHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_):  # quiet; the mesh access log is the source of truth
        pass

    def _handle(self):
        length = int(self.headers.get("content-length") or 0)
        if length:
            self.rfile.read(length)  # body may contain card/account data: never logged
        f = current()
        if f["mode"] == "down":
            # Closing the listener only stops NEW connections; Envoy would keep using its
            # pooled keep-alive ones. Drop those too, so "down" is a real outage.
            self.close_connection = True
            self.connection.shutdown(socket.SHUT_RDWR)
            return
        if f["mode"] == "unreachable":
            time.sleep(3600)
            return
        time.sleep(injected_delay(f))
        if random.random() < f["error_rate"]:
            code, body = 503, {"error": f"{NAME} internal error (injected)"}
        else:
            code, body = RESPONSES.get(NAME, lambda: (200, {"status": "ok"}))()
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_GET = do_POST = do_PUT = _handle


# ------------------------------------------------------------- TCP proxy mock
class ProxyHandler(BaseRequestHandler):
    def handle(self):
        f = current()
        client = self.request
        if f["mode"] == "unreachable":
            time.sleep(3600)
            return
        if random.random() < f["error_rate"]:
            # abortive close (RST), like a DB listener that is overloaded or crashing
            client.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, b"\x01\x00\x00\x00\x00\x00\x00\x00")
            client.close()
            return
        time.sleep(injected_delay(f))
        host, port = UPSTREAM.rsplit(":", 1)
        try:
            upstream = socket.create_connection((host, int(port)), timeout=5)
        except OSError as e:
            log(f"upstream connect failed: {e}")
            return
        conns = [client, upstream]
        try:
            while current()["mode"] == "up":
                ready, _, _ = select.select(conns, [], [], 1.0)
                for s in ready:
                    data = s.recv(65536)
                    if not data:
                        return
                    if s is client:
                        lat = current()["latency_ms"]
                        if lat:  # slow every query, not just the connect
                            time.sleep(injected_delay(current()))
                        upstream.sendall(data)
                    else:
                        client.sendall(data)
        except OSError:
            pass
        finally:
            upstream.close()


# ------------------------------------------------------- listener on/off switch
class Listener:
    """Owns the service socket so "down" really closes it (connection refused)."""

    def __init__(self):
        self.server = None

    def start(self):
        if self.server:
            return
        if MODE == "tcp-proxy":
            ThreadingTCPServer.allow_reuse_address = True
            ThreadingTCPServer.daemon_threads = True
            srv = ThreadingTCPServer(("0.0.0.0", PORT), ProxyHandler)
        else:
            ThreadingHTTPServer.daemon_threads = True
            srv = ThreadingHTTPServer(("0.0.0.0", PORT), MockHandler)
            if TLS_CERT:
                ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                ctx.load_cert_chain(TLS_CERT, TLS_KEY)
                srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.server = srv
        log(f"listening on :{PORT} ({MODE}{', TLS' if TLS_CERT and MODE == 'http' else ''})")

    def stop(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.server = None
            log("listener closed (down)")


listener = Listener()


def apply(new: dict) -> dict:
    with lock:
        for k in ("latency_ms", "jitter_ms", "error_rate"):
            if k in new:
                fault[k] = float(new[k])
        if new.get("mode") in ("up", "down", "unreachable"):
            fault["mode"] = new["mode"]
        state = dict(fault)
    listener.stop() if state["mode"] == "down" else listener.start()
    log(f"fault set: {state}")
    return state


class AdminHandler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def _send(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._send(200, {"name": NAME, "mode_type": MODE, "fault": current()})

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("content-length") or 0)) or b"{}"
        try:
            req = json.loads(body)
        except ValueError:
            return self._send(400, {"error": "invalid json"})
        if self.path.startswith("/reset"):
            state = apply(dict(HEALTHY))
        elif self.path.startswith("/fault"):
            state = apply(req)
        else:
            return self._send(404, {"error": "use /fault or /reset"})
        self._send(200, {"name": NAME, "fault": state})


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    listener.start()
    log(f"admin API on :{ADMIN_PORT}")
    ThreadingHTTPServer(("0.0.0.0", ADMIN_PORT), AdminHandler).serve_forever()
