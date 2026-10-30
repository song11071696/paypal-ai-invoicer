"""Command line: `python -m paste2pay serve|demo|extract FILE`."""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys

from .agent import Agent
from .extract import get_extractor
from .paypal import MockPayPal, get_paypal_client
from .store import Ledger

SAMPLES = pathlib.Path(__file__).resolve().parent.parent / "samples"


def _load_dotenv():
    p = pathlib.Path(".env")
    if p.exists():
        for line in p.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _p(title, obj=None):
    print(f"\n\033[1m== {title}\033[0m")
    if obj is not None:
        print(json.dumps(obj, indent=2, ensure_ascii=False) if not isinstance(obj, str) else obj)


def demo():
    agent = Agent(get_extractor(), get_paypal_client(), Ledger(None))
    _p(f"Paste2Pay demo  |  PayPal: {agent.mode}  |  AI engine: {agent.extractor.name}")
    recs = {}
    for name in ("chat_order", "ocr_invoice", "dm_no_email"):
        text = (SAMPLES / f"{name}.txt").read_text(encoding="utf-8")
        _p(f"[1] Paste: samples/{name}.txt", text.strip())
        rec = agent.process(text)
        recs[name] = rec
        for step in rec["trace"]:
            print(f"  - {step['step']}: {step['detail']}")
            for c in step.get("calls", []):
                print(f"      PayPal {c['method']} {c['path']} -> {c['status']}")
        print(f"  => {rec['kind']} {rec['paypal_id']} total {rec['total']} {rec['currency']} "
              f"status {rec['status']}  pay link: {rec['pay_link']}")

    _p("[2] Reconcile pasted payment notices (one is fake)")
    text = (SAMPLES / "payments.txt").read_text(encoding="utf-8")
    for r in agent.match_payments(text)["results"]:
        p = r["payment"]
        print(f"  - {p['amount']} {p['currency']} from {p['payer']} [{p['method']}] -> {r['record_id']} "
              f"(score {r['score']}): {r['action']}")

    if isinstance(agent.paypal, MockPayPal):
        _p("[3] Buyers pay (simulated in mock mode) and the agent syncs with PayPal")
        agent.simulate_payment(recs["chat_order"]["id"])
        agent.simulate_payment(recs["dm_no_email"]["id"])
        for c in agent.sync()["changes"]:
            print(f"  - {c['id']}: {c['from']} -> {c['to']}")
    else:
        _p("[3] Sandbox: open the pay links with a sandbox *personal* account, then run sync in the web UI")

    _p("[4] Ledger summary", agent.summary())


def main(argv=None):
    _load_dotenv()
    ap = argparse.ArgumentParser(prog="paste2pay")
    sub = ap.add_subparsers(dest="cmd")
    s = sub.add_parser("serve", help="run the web app")
    s.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8000)))
    s.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    sub.add_parser("demo", help="scripted end-to-end run in the terminal")
    e = sub.add_parser("extract", help="AI extraction only")
    e.add_argument("file")
    a = ap.parse_args(argv)
    if a.cmd == "serve":
        from .server import serve
        serve(a.port, a.host)
    elif a.cmd == "demo":
        demo()
    elif a.cmd == "extract":
        from .extract import finalize
        text = sys.stdin.read() if a.file == "-" else pathlib.Path(a.file).read_text(encoding="utf-8")
        print(json.dumps(finalize(get_extractor().extract(text)), indent=2, ensure_ascii=False))
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
