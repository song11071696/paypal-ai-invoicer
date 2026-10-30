"""Zero-dependency web server (stdlib http.server) exposing the agent as a JSON API + one-page UI."""
from __future__ import annotations

import json
import os
import pathlib
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .agent import Agent
from .extract import get_extractor
from .paypal import MockPayPal, PayPalError, get_paypal_client
from .store import Ledger

ROOT = pathlib.Path(__file__).resolve().parent
SAMPLES = ROOT.parent / "samples"


def build_agent(env=None, port: int = 8000) -> Agent:
    env = os.environ if env is None else env
    return Agent(get_extractor(env), get_paypal_client(env), Ledger(env.get("LEDGER_PATH") or "data/ledger.json"),
                 public_url=env.get("PUBLIC_URL") or f"http://localhost:{port}",
                 brand_name=env.get("BRAND_NAME"))


def make_handler(agent: Agent):
    class Handler(BaseHTTPRequestHandler):
        server_version = "Paste2Pay/1.0"

        def log_message(self, fmt, *args):  # quieter console
            if os.environ.get("P2P_VERBOSE"):
                super().log_message(fmt, *args)

        def _send(self, code, payload, ctype="application/json"):
            body = payload if isinstance(payload, (bytes, bytearray)) else json.dumps(payload, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype + ("; charset=utf-8" if "json" in ctype or "html" in ctype else ""))
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _redirect(self, to):
            self.send_response(302)
            self.send_header("Location", to)
            self.end_headers()

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            if not n:
                return {}
            return json.loads(self.rfile.read(n).decode() or "{}")

        def do_GET(self):
            u = urlparse(self.path)
            q = parse_qs(u.query)
            if u.path in ("/", "/index.html"):
                return self._send(200, (ROOT / "static" / "index.html").read_bytes(), "text/html")
            if u.path == "/api/status":
                return self._send(200, {"paypal_mode": agent.mode, "ai_engine": agent.extractor.name,
                                        "paypal_base": getattr(agent.paypal, "base_url", "mock (in-memory)")})
            if u.path == "/api/samples":
                out = {p.stem: p.read_text(encoding="utf-8") for p in sorted(SAMPLES.glob("*.txt"))}
                return self._send(200, out)
            if u.path == "/api/records":
                return self._send(200, {"records": agent.ledger.all(), "summary": agent.summary()})
            if u.path.startswith("/api/records/") and u.path.endswith("/reminder"):
                rid = u.path.split("/")[3]
                return self._send(200, {"text": agent.reminder(rid)})
            if u.path == "/paypal/return":
                token = (q.get("token") or [""])[0]
                rec = agent.handle_return(token)
                return self._redirect(f"/?paid={rec['id'] if rec else ''}")
            if u.path == "/paypal/cancel":
                return self._redirect("/?cancelled=1")
            return self._send(404, {"error": "not found"})

        def do_POST(self):
            u = urlparse(self.path)
            try:
                data = self._body()
                if u.path == "/api/extract":
                    return self._send(200, agent.extract(data.get("text", "")))
                if u.path == "/api/process":
                    rec = agent.process(data.get("text", ""), data.get("mode", "auto"), data.get("extraction"))
                    return self._send(200, rec)
                if u.path == "/api/sync":
                    return self._send(200, agent.sync())
                if u.path == "/api/payments":
                    return self._send(200, agent.match_payments(data.get("text", "")))
                if u.path.startswith("/api/simulate/"):
                    rid = u.path.rsplit("/", 1)[-1]
                    if not isinstance(agent.paypal, MockPayPal):
                        return self._send(400, {"error": "simulate only works in mock mode"})
                    return self._send(200, agent.simulate_payment(rid, data.get("amount")))
                if u.path == "/api/reset":
                    agent.ledger.clear()
                    return self._send(200, {"ok": True})
            except PayPalError as e:
                return self._send(502, {"error": str(e), "debug_id": e.debug_id})
            except Exception as e:  # keep the demo alive and show the error
                return self._send(500, {"error": f"{type(e).__name__}: {e}"})
            return self._send(404, {"error": "not found"})

    return Handler


def serve(port: int = 8000, host: str = "127.0.0.1", agent: Agent | None = None, block: bool = True):
    agent = agent or build_agent(port=port)
    httpd = ThreadingHTTPServer((host, port), make_handler(agent))
    if not block:
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        return httpd
    print(f"Paste2Pay running on http://{host}:{httpd.server_port}  "
          f"(PayPal: {agent.mode}, AI: {agent.extractor.name})")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return httpd
