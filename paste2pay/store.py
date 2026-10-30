"""Tiny JSON-file ledger (or in-memory when path is None)."""
from __future__ import annotations

import json
import os
import threading


class Ledger:
    def __init__(self, path: str | None = None):
        self.path = path
        self._lock = threading.RLock()
        self.records: dict[str, dict] = {}
        if path and os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                self.records = {r["id"]: r for r in json.load(f)}

    def save(self, rec: dict) -> dict:
        with self._lock:
            self.records[rec["id"]] = rec
            self._flush()
        return rec

    def get(self, rid: str) -> dict | None:
        return self.records.get(rid)

    def find(self, **kw) -> dict | None:
        for r in self.records.values():
            if all(r.get(k) == v for k, v in kw.items()):
                return r
        return None

    def all(self) -> list[dict]:
        return sorted(self.records.values(), key=lambda r: r["created_at"], reverse=True)

    def clear(self):
        with self._lock:
            self.records = {}
            self._flush()

    def _flush(self):
        if not self.path:
            return
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(list(self.records.values()), f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)
