import json
import sqlite3
import time
from pathlib import Path


class Store:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("CREATE TABLE IF NOT EXISTS score_events (event_id TEXT PRIMARY KEY, set_id TEXT, side TEXT, before_left INTEGER, before_right INTEGER, status TEXT, created REAL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS event_log (id INTEGER PRIMARY KEY, created REAL, kind TEXT, detail TEXT)")
        self.db.execute("CREATE TABLE IF NOT EXISTS profiles (key TEXT PRIMARY KEY, value TEXT, expires REAL)")
        self.db.commit()

    def log(self, kind: str, detail: dict) -> None:
        self.db.execute("INSERT INTO event_log(created,kind,detail) VALUES(?,?,?)",
                        (time.time(), kind, json.dumps(detail)))
        self.db.commit()

    def recent(self, limit: int = 30) -> list[dict]:
        return [dict(row) for row in self.db.execute(
            "SELECT created,kind,detail FROM event_log ORDER BY id DESC LIMIT ?", (limit,))]

    def event(self, event_id: str) -> dict | None:
        row = self.db.execute("SELECT * FROM score_events WHERE event_id=?", (event_id,)).fetchone()
        return dict(row) if row else None

    def start_score(self, event_id: str, set_id: str, side: str, left: int, right: int) -> bool:
        try:
            self.db.execute("INSERT INTO score_events VALUES(?,?,?,?,?,?,?)",
                            (event_id, set_id, side, left, right, "pending", time.time()))
            self.db.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def finish_score(self, event_id: str, status: str) -> None:
        self.db.execute("UPDATE score_events SET status=? WHERE event_id=?", (status, event_id))
        self.db.commit()

    def last_score(self) -> dict | None:
        row = self.db.execute("SELECT * FROM score_events WHERE status='applied' ORDER BY created DESC LIMIT 1").fetchone()
        return dict(row) if row else None

    def cache_get(self, key: str) -> dict | None:
        row = self.db.execute("SELECT value,expires FROM profiles WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row and row["expires"] > time.time() else None

    def cache_entry(self, key: str) -> tuple[dict, float] | None:
        row = self.db.execute("SELECT value,expires FROM profiles WHERE key=?", (key,)).fetchone()
        return (json.loads(row["value"]), float(row["expires"])) if row else None

    def cache_put(self, key: str, value: dict, ttl: float) -> None:
        self.db.execute("INSERT OR REPLACE INTO profiles VALUES(?,?,?)",
                        (key, json.dumps(value), time.time() + ttl))
        self.db.commit()
