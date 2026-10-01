"""Small durable task journal. Credentials are never stored in task records."""

import json
import sqlite3
import threading
import time
from pathlib import Path
from uuid import uuid4


ACTIVE = {"queued", "running", "publishing"}


class TaskStore:
    def __init__(self, path: Path):
        self.lock = threading.RLock()
        self.connection = sqlite3.connect(path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("""CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY, kind TEXT NOT NULL, title TEXT NOT NULL,
            status TEXT NOT NULL, params TEXT NOT NULL, payload TEXT NOT NULL,
            created REAL NOT NULL, updated REAL NOT NULL)""")
        self.connection.execute("""CREATE TABLE IF NOT EXISTS logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, message TEXT)""")
        self.connection.commit()

    def create(self, kind, params):
        task_id = str(uuid4())
        now = time.time()
        title = (
            params.get("video_subject") or params.get("video_script", "")[:60] or kind
        )
        with self.lock, self.connection:
            self.connection.execute(
                "INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?)",
                (
                    task_id,
                    kind,
                    title,
                    "queued",
                    json.dumps(params, ensure_ascii=False),
                    "{}",
                    now,
                    now,
                ),
            )
        return self.get(task_id)

    def get(self, task_id):
        with self.lock:
            row = self.connection.execute(
                "SELECT * FROM tasks WHERE id=?", (task_id,)
            ).fetchone()
        if not row:
            return None
        result = dict(row)
        result["params"] = json.loads(result["params"])
        result["payload"] = json.loads(result["payload"])
        return result

    def list(self, limit=100):
        with self.lock:
            rows = self.connection.execute(
                "SELECT id FROM tasks ORDER BY created DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self.get(row["id"]) for row in rows]

    def update(self, task_id, status=None, **payload):
        with self.lock:
            task = self.get(task_id)
            if not task:
                return
            task["payload"].update(payload)
            with self.connection:
                self.connection.execute(
                    "UPDATE tasks SET status=?,payload=?,updated=? WHERE id=?",
                    (
                        status or task["status"],
                        json.dumps(task["payload"], ensure_ascii=False),
                        time.time(),
                        task_id,
                    ),
                )

    def active(self):
        with self.lock:
            rows = self.connection.execute(
                "SELECT id FROM tasks WHERE status IN ('queued','running','publishing') ORDER BY created"
            ).fetchall()
        return [self.get(row["id"]) for row in rows]

    def log(self, task_id, message):
        with self.lock, self.connection:
            self.connection.execute(
                "INSERT INTO logs(task_id,message) VALUES (?,?)",
                (task_id, message[:8000]),
            )
            self.connection.execute(
                """DELETE FROM logs WHERE task_id=? AND id NOT IN
                (SELECT id FROM logs WHERE task_id=? ORDER BY id DESC LIMIT 1000)""",
                (task_id, task_id),
            )

    def logs(self, task_id):
        with self.lock:
            return [
                row[0]
                for row in self.connection.execute(
                    "SELECT message FROM logs WHERE task_id=? ORDER BY id", (task_id,)
                )
            ]

    def recover(self):
        for task in self.active():
            self.update(
                task["id"],
                "interrupted",
                error="程序退出时任务未完成。检查已有产物及远端任务后再重试。",
            )

    def delete(self, task_id):
        with self.lock, self.connection:
            self.connection.execute("DELETE FROM logs WHERE task_id=?", (task_id,))
            self.connection.execute("DELETE FROM tasks WHERE id=?", (task_id,))

    def close(self):
        with self.lock:
            self.connection.close()
