"""SQLite persistence for outbound calls and the append-only audit log (stdlib sqlite3, one connection guarded by a lock).

What is stored: masked destination + keyed hash (never the full number), consent record, status timestamps, provider ids,
HASHES of the one-time tokens (never the tokens). What is never stored: audio, transcripts, credentials, full numbers.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from .models import TERMINAL, CallStatus, can_transition

SCHEMA = """
CREATE TABLE IF NOT EXISTS outbound_calls (
    call_id TEXT PRIMARY KEY, requested_by TEXT NOT NULL, requester_role TEXT NOT NULL,
    idempotency_key TEXT NOT NULL, request_fp TEXT NOT NULL,
    dest_masked TEXT NOT NULL, dest_hash TEXT NOT NULL, country_code TEXT NOT NULL, lang TEXT NOT NULL,
    consent_basis TEXT NOT NULL, consent_note TEXT NOT NULL DEFAULT '', consent_at REAL NOT NULL,
    status TEXT NOT NULL, created_at REAL NOT NULL, dialing_at REAL, answered_at REAL, connected_at REAL, ended_at REAL,
    duration_s INTEGER, failure_reason TEXT, outcome TEXT,
    provider TEXT NOT NULL, provider_sid TEXT, stream_sid TEXT,
    ws_token_hash TEXT, ws_token_expires REAL, ws_started INTEGER NOT NULL DEFAULT 0, cb_token_hash TEXT,
    cancel_requested_at REAL, cancel_reason TEXT, hangup_confirmed INTEGER NOT NULL DEFAULT 0,
    ops_call_id TEXT,
    UNIQUE (requested_by, idempotency_key)
);
CREATE INDEX IF NOT EXISTS ix_oc_user ON outbound_calls (requested_by, created_at);
CREATE INDEX IF NOT EXISTS ix_oc_dest ON outbound_calls (dest_hash, created_at);
CREATE INDEX IF NOT EXISTS ix_oc_status ON outbound_calls (status);
CREATE TABLE IF NOT EXISTS outbound_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, call_id TEXT, event TEXT NOT NULL, actor TEXT, detail TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS ix_audit_call ON outbound_audit (call_id, id);
"""

COLUMNS = ("call_id requested_by requester_role idempotency_key request_fp dest_masked dest_hash country_code lang consent_basis consent_note "
           "consent_at status created_at dialing_at answered_at connected_at ended_at duration_s failure_reason outcome provider provider_sid "
           "stream_sid ws_token_hash ws_token_expires ws_started cb_token_hash cancel_requested_at cancel_reason hangup_confirmed ops_call_id").split()


class OutboundStore:
    def __init__(self, path: str | Path) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._db.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # ---------------------------------------------------------------- calls
    def insert(self, row: dict[str, Any]) -> None:
        cols = [c for c in COLUMNS if c in row]
        with self._lock:
            self._db.execute(f"INSERT INTO outbound_calls ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", [row[c] for c in cols])

    def get(self, call_id: str) -> dict[str, Any] | None:
        with self._lock:
            r = self._db.execute("SELECT * FROM outbound_calls WHERE call_id=?", (call_id,)).fetchone()
        return dict(r) if r else None

    def get_by_idempotency(self, user: str, key: str) -> dict[str, Any] | None:
        with self._lock:
            r = self._db.execute("SELECT * FROM outbound_calls WHERE requested_by=? AND idempotency_key=?", (user, key)).fetchone()
        return dict(r) if r else None

    def update(self, call_id: str, **fields: Any) -> None:
        """Non-status fields only; use set_status for transitions."""
        assert "status" not in fields
        cols = [c for c in fields if c in COLUMNS]
        with self._lock:
            self._db.execute(f"UPDATE outbound_calls SET {','.join(c + '=?' for c in cols)} WHERE call_id=?", [fields[c] for c in cols] + [call_id])

    def set_status(self, call_id: str, new: CallStatus, now: float, **fields: Any) -> bool:
        """Apply a legal transition atomically. Returns False (and changes nothing) if it is not allowed from the current status."""
        with self._lock:
            row = self.get(call_id)
            if row is None or not can_transition(CallStatus(row["status"]), new):
                return False
            stamp = {"dialing": "dialing_at", "answered": "answered_at", "connected": "connected_at"}.get(new.value)
            if new in TERMINAL:
                stamp = "ended_at"
            sets = {"status": new.value, **fields}
            if stamp:
                sets[stamp] = now
            if new in TERMINAL and "duration_s" not in sets:
                begin = row["connected_at"] or row["answered_at"]
                sets["duration_s"] = int(max(0, now - begin)) if begin else 0
            cols = [c for c in sets if c in COLUMNS]
            self._db.execute(f"UPDATE outbound_calls SET {','.join(c + '=?' for c in cols)} WHERE call_id=?", [sets[c] for c in cols] + [call_id])
            return True

    def find_by_token(self, kind: str, token_hash: str) -> dict[str, Any] | None:
        col = {"ws": "ws_token_hash", "cb": "cb_token_hash"}[kind]
        with self._lock:
            r = self._db.execute(f"SELECT * FROM outbound_calls WHERE {col}=?", (token_hash,)).fetchone()
        return dict(r) if r else None

    # ---------------------------------------------------------------- limits
    def count_user_since(self, user: str, since: float) -> int:
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM outbound_calls WHERE requested_by=? AND created_at>=?", (user, since)).fetchone()[0]

    def count_destination_since(self, dest_hash: str, since: float) -> int:
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM outbound_calls WHERE dest_hash=? AND created_at>=?", (dest_hash, since)).fetchone()[0]

    def active_for_destination(self, dest_hash: str, since: float) -> bool:
        terminal = ",".join(f"'{s.value}'" for s in TERMINAL)
        with self._lock:
            return self._db.execute(f"SELECT 1 FROM outbound_calls WHERE dest_hash=? AND (status NOT IN ({terminal}) OR created_at>=?) LIMIT 1",
                                    (dest_hash, since)).fetchone() is not None

    def count_active(self) -> int:
        terminal = ",".join(f"'{s.value}'" for s in TERMINAL)
        with self._lock:
            return self._db.execute(f"SELECT COUNT(*) FROM outbound_calls WHERE status NOT IN ({terminal})").fetchone()[0]

    def active_calls(self) -> list[dict[str, Any]]:
        terminal = ",".join(f"'{s.value}'" for s in TERMINAL)
        with self._lock:
            return [dict(r) for r in self._db.execute(f"SELECT * FROM outbound_calls WHERE status NOT IN ({terminal}) ORDER BY created_at").fetchall()]

    def seconds_used_since(self, since: float) -> int:
        with self._lock:
            return int(self._db.execute("SELECT COALESCE(SUM(duration_s),0) FROM outbound_calls WHERE created_at>=?", (since,)).fetchone()[0])

    def history(self, limit: int, before: float | None = None, status: str | None = None, user: str | None = None) -> list[dict[str, Any]]:
        q, args = "SELECT * FROM outbound_calls WHERE 1=1", []
        if before is not None:
            q += " AND created_at<?"
            args.append(before)
        if status:
            q += " AND status=?"
            args.append(status)
        if user:
            q += " AND requested_by=?"
            args.append(user)
        q += " ORDER BY created_at DESC, call_id DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            return [dict(r) for r in self._db.execute(q, args).fetchall()]

    # ---------------------------------------------------------------- audit + retention
    def audit(self, ts: float, call_id: str | None, event: str, actor: str | None = None, **detail: Any) -> None:
        with self._lock:
            self._db.execute("INSERT INTO outbound_audit (ts, call_id, event, actor, detail) VALUES (?,?,?,?,?)",
                             (ts, call_id, event, actor, json.dumps(detail, ensure_ascii=False, default=str)))

    def audit_rows(self, call_id: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM outbound_audit WHERE (? IS NULL OR call_id=?) ORDER BY id", (call_id, call_id)).fetchall()
        return [{**dict(r), "detail": json.loads(r["detail"])} for r in rows]

    def purge_consent_notes(self, older_than: float) -> int:
        """Consent retention: free-text notes are blanked after the retention period (the structured consent record stays for audit)."""
        with self._lock:
            cur = self._db.execute("UPDATE outbound_calls SET consent_note='' WHERE consent_at<? AND consent_note<>''", (older_than,))
            return cur.rowcount
