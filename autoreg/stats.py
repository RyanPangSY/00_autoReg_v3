"""Per-user booking history + usage stats (SQLite, stdlib only).

Every per-slot booking attempt is recorded in data/autoreg.db:

    id, username, booked_at, machine, slot_start (ISO), status, intent_id

status is one of BOOKED / FAILED / DRY_RUN. Stats count only BOOKED slots;
"days registered" = distinct calendar days with at least one BOOKED slot.
"""
import os
import sqlite3
import threading


class StatsDB:
    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS bookings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL,
                booked_at TEXT NOT NULL,
                machine TEXT NOT NULL,
                slot_start TEXT,
                status TEXT NOT NULL,
                intent_id TEXT
            )
            """
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_bookings_user ON bookings (username)"
        )
        self.conn.commit()

    def record(self, username, machine, slot_start, status, intent_id=None):
        with self._lock:
            self.conn.execute(
                "INSERT INTO bookings (username, booked_at, machine, slot_start, status, intent_id) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (username, _now_iso(), machine, slot_start, status, intent_id),
            )
            self.conn.commit()

    def user_stats(self, username):
        """Stats for one user (BOOKED slots only)."""
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*), COUNT(DISTINCT substr(slot_start, 1, 10)) "
                "FROM bookings WHERE username = ? AND status = 'BOOKED'",
                (username,),
            ).fetchone()
            by_machine = dict(
                self.conn.execute(
                    "SELECT machine, COUNT(*) FROM bookings "
                    "WHERE username = ? AND status = 'BOOKED' GROUP BY machine",
                    (username,),
                ).fetchall()
            )
            recent = [
                {"slot_start": r[0], "machine": r[1], "status": r[2]}
                for r in self.conn.execute(
                    "SELECT slot_start, machine, status FROM bookings "
                    "WHERE username = ? ORDER BY id DESC LIMIT 10",
                    (username,),
                ).fetchall()
            ]
        return {
            "bookings": row[0],
            "distinct_days": row[1],
            "by_machine": by_machine,
            "recent": recent,
        }

    def all_stats(self):
        """Per-user summary for the admin view."""
        with self._lock:
            rows = self.conn.execute(
                "SELECT username, COUNT(*), COUNT(DISTINCT substr(slot_start, 1, 10)) "
                "FROM bookings WHERE status = 'BOOKED' GROUP BY username"
            ).fetchall()
        return [
            {"username": u, "bookings": b, "distinct_days": d} for u, b, d in rows
        ]


def _now_iso():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()
