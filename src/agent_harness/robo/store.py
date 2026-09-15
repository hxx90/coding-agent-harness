"""Durable objects and ordered evidence, owned by one execution host."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from pathlib import Path

from agent_harness.coding.types import CodingError, Json


def canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


class Store:
    def __init__(self, root: Path) -> None:
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root = root
        self.lock = threading.RLock()
        self.db = sqlite3.connect(root / "robo.sqlite3", check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS objects (
                kind TEXT, id TEXT, body TEXT NOT NULL, PRIMARY KEY(kind,id));
            CREATE TABLE IF NOT EXISTS events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT, body TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS job_events ON events(job_id,seq);
        """)

    def get(self, kind: str, key: str) -> Json:
        with self.lock:
            row = self.db.execute(
                "SELECT body FROM objects WHERE kind=? AND id=?", (kind, key)
            ).fetchone()
        if row is None:
            raise CodingError("not_found", f"Unknown {kind}: {key}")
        return dict(json.loads(row[0]))

    def all(self, kind: str) -> list[Json]:
        with self.lock:
            return [
                json.loads(row[0])
                for row in self.db.execute(
                    "SELECT body FROM objects WHERE kind=? ORDER BY rowid", (kind,)
                )
            ]

    def put(self, kind: str, key: str, value: Json, *, immutable: bool = False) -> None:
        body = canonical(value)
        with self.lock, self.db:
            old = self.db.execute(
                "SELECT body FROM objects WHERE kind=? AND id=?", (kind, key)
            ).fetchone()
            if immutable and old:
                if old[0] != body:
                    raise CodingError(
                        "immutable_conflict", f"Cannot replace {kind}: {key}"
                    )
                return
            self.db.execute(
                "INSERT OR REPLACE INTO objects VALUES(?,?,?)", (kind, key, body)
            )

    def event(self, job: Json | None, kind: str, **data: object) -> Json:
        value: Json = {"type": kind, "timestamp": time.time(), **data}
        if job:
            value.update(job_id=job["id"], program_version=job["program_version"])
        with self.lock, self.db:
            cursor = self.db.execute(
                "INSERT INTO events(job_id,body) VALUES(?,?)",
                (value.get("job_id"), canonical(value)),
            )
            value["seq"] = cursor.lastrowid
        return value

    def create_job(self, job: Json, fingerprint: str) -> None:
        """Commit the reconnect key and job before starting any physical work."""
        with self.lock, self.db:
            self.db.executemany(
                "INSERT INTO objects VALUES(?,?,?)",
                [
                    ("job", job["id"], canonical(job)),
                    (
                        "request",
                        job["request_id"],
                        canonical({"fingerprint": fingerprint, "job_id": job["id"]}),
                    ),
                ],
            )

    def events(self, job_id: str, after: int, limit: int = 100) -> Json:
        with self.lock:
            rows = self.db.execute(
                "SELECT seq,body FROM events WHERE job_id=? AND seq>? ORDER BY seq LIMIT ?",
                (job_id, after, limit),
            ).fetchall()
        events = [{**json.loads(body), "seq": seq} for seq, body in rows]
        return {"events": events, "cursor": rows[-1][0] if rows else after}
