"""SQLite task queue. Opens a fresh connection per call so the poller, runner
and web threads never share one.

A conversation is a root task plus follow-up tasks whose parent_id points at
the root. Every task is one unit of Claude work; follow-ups continue the
previous task's Claude session (or a fresh one when its context got too big).
"""

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    parent_id INTEGER REFERENCES tasks(id),
    message_id TEXT UNIQUE NOT NULL,
    source TEXT NOT NULL,
    sender TEXT NOT NULL,
    subject TEXT NOT NULL,
    project_dir TEXT NOT NULL,
    session_id TEXT NOT NULL,
    prompt TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    attempts INTEGER NOT NULL DEFAULT 0,
    retry_after TEXT,
    question TEXT,
    waiting_since TEXT,
    result TEXT,
    error TEXT,
    cost_usd REAL,
    context_tokens INTEGER,
    tokens_in INTEGER,
    tokens_out INTEGER,
    limit_used REAL,
    model TEXT,
    model_choice TEXT,
    model_reason TEXT,
    model_usage TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS seen (
    message_id TEXT PRIMARY KEY,
    note TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sent (
    message_id TEXT PRIMARY KEY,
    root_id INTEGER NOT NULL
);
"""

MIGRATIONS = {"tokens_in": "INTEGER", "tokens_out": "INTEGER", "limit_used": "REAL", "model": "TEXT",
              "model_choice": "TEXT", "model_reason": "TEXT", "model_usage": "TEXT"}

# 'running' is included so a task interrupted by a reboot is resumed.
RUNNABLE = ("running", "queued", "rate_limited")
_THREAD = "COALESCE({0}.parent_id, {0}.id)"


@dataclass
class Task:
    id: int
    parent_id: int | None
    message_id: str
    source: str  # mail | panel | timeout
    sender: str
    subject: str
    project_dir: Path
    session_id: str  # '' for a follow-up that has not been started yet
    prompt: str
    status: str  # queued | running | waiting | done | failed | stopped | rate_limited
    attempts: int
    retry_after: str | None
    question: str | None
    waiting_since: str | None
    result: str | None
    error: str | None
    cost_usd: float | None
    context_tokens: int | None
    tokens_in: int | None  # includes cache reads/writes
    tokens_out: int | None
    limit_used: float | None  # share of the 5-hour subscription window, 0..1
    model: str | None
    model_choice: str | None  # 'opus/high': what we asked for
    model_reason: str | None
    model_usage: str | None  # JSON of the result's modelUsage (sub-agents included)
    created_at: str
    updated_at: str

    @property
    def root_id(self) -> int:
        return self.parent_id or self.id


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class Store:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        with closing(self._connect()) as db, db:
            db.executescript(SCHEMA)
            # Databases made by an older version lack the newer columns.
            existing = {row[1] for row in db.execute("PRAGMA table_info(tasks)")}
            for name, decl in MIGRATIONS.items():
                if name not in existing:
                    db.execute(f"ALTER TABLE tasks ADD COLUMN {name} {decl}")

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.db_path, timeout=30)
        db.row_factory = sqlite3.Row
        return db

    @staticmethod
    def _task(row: sqlite3.Row) -> Task:
        data = dict(row)
        data["project_dir"] = Path(data["project_dir"])
        return Task(**data)

    def _one(self, sql: str, params: tuple = ()) -> Task | None:
        with closing(self._connect()) as db:
            row = db.execute(sql, params).fetchone()
        return self._task(row) if row else None

    def _all(self, sql: str, params: tuple = ()) -> list[Task]:
        with closing(self._connect()) as db:
            rows = db.execute(sql, params).fetchall()
        return [self._task(r) for r in rows]

    def is_known(self, message_id: str) -> bool:
        with closing(self._connect()) as db:
            return bool(
                db.execute("SELECT 1 FROM tasks WHERE message_id=?", (message_id,)).fetchone()
                or db.execute("SELECT 1 FROM seen WHERE message_id=?", (message_id,)).fetchone()
            )

    def mark_ignored(self, message_id: str, note: str) -> None:
        with closing(self._connect()) as db, db:
            db.execute("INSERT OR IGNORE INTO seen VALUES (?, ?, ?)", (message_id, note, _now()))

    def add_task(
        self,
        *,
        message_id: str,
        source: str,
        sender: str,
        subject: str,
        project_dir: Path,
        session_id: str,
        prompt: str,
        parent_id: int | None = None,
    ) -> int:
        now = _now()
        with closing(self._connect()) as db, db:
            cur = db.execute(
                "INSERT INTO tasks (parent_id, message_id, source, sender, subject, project_dir,"
                " session_id, prompt, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (parent_id, message_id, source, sender, subject, str(project_dir), session_id, prompt, now, now),
            )
            return cur.lastrowid

    def next_runnable(self) -> Task | None:
        """Oldest runnable task whose conversation has no earlier unfinished task,
        so messages of one conversation run in order."""
        marks = ",".join("?" * len(RUNNABLE))
        return self._one(
            f"SELECT * FROM tasks t WHERE t.status IN ({marks})"
            " AND (t.retry_after IS NULL OR t.retry_after <= ?)"
            f" AND NOT EXISTS (SELECT 1 FROM tasks p WHERE {_THREAD.format('p')} = {_THREAD.format('t')}"
            f"   AND p.id < t.id AND p.status IN ({marks}))"
            " ORDER BY CASE t.status WHEN 'running' THEN 0 ELSE 1 END, t.id LIMIT 1",
            (*RUNNABLE, _now(), *RUNNABLE),
        )

    def get(self, task_id: int) -> Task | None:
        return self._one("SELECT * FROM tasks WHERE id=?", (task_id,))

    def recent(self, limit: int = 200) -> list[Task]:
        return self._all("SELECT * FROM tasks ORDER BY id DESC LIMIT ?", (limit,))

    def thread(self, root_id: int) -> list[Task]:
        return self._all("SELECT * FROM tasks WHERE id=? OR parent_id=? ORDER BY id", (root_id, root_id))

    def previous(self, task: Task) -> Task | None:
        """Latest earlier task of the same conversation that has a session."""
        return self._one(
            "SELECT * FROM tasks WHERE (id=? OR parent_id=?) AND id < ? AND session_id != ''"
            " ORDER BY id DESC LIMIT 1",
            (task.root_id, task.root_id, task.id),
        )

    def update(self, task_id: int, **fields: object) -> None:
        fields["updated_at"] = _now()
        columns = ", ".join(f"{k}=?" for k in fields)
        with closing(self._connect()) as db, db:
            db.execute(f"UPDATE tasks SET {columns} WHERE id=?", (*fields.values(), task_id))

    def start(self, task_id: int) -> None:
        with closing(self._connect()) as db, db:
            db.execute(
                "UPDATE tasks SET status='running', attempts=attempts+1, retry_after=NULL,"
                " updated_at=? WHERE id=?",
                (_now(), task_id),
            )

    def postpone(self, task_id: int, minutes: int, reason: str) -> None:
        retry = (datetime.now() + timedelta(minutes=minutes)).isoformat(timespec="seconds")
        self.update(task_id, status="rate_limited", retry_after=retry, error=reason)

    def close_waiting(self, root_id: int) -> None:
        """A new message answers any open question of the conversation."""
        with closing(self._connect()) as db, db:
            db.execute(
                "UPDATE tasks SET status='done', updated_at=? WHERE (id=? OR parent_id=?) AND status='waiting'",
                (_now(), root_id, root_id),
            )

    def expired_questions(self, hours: int) -> list[Task]:
        limit = (datetime.now() - timedelta(hours=hours)).isoformat(timespec="seconds")
        return self._all("SELECT * FROM tasks WHERE status='waiting' AND waiting_since <= ?", (limit,))

    def record_sent(self, message_id: str, root_id: int) -> None:
        with closing(self._connect()) as db, db:
            db.execute("INSERT OR IGNORE INTO sent VALUES (?, ?)", (message_id, root_id))

    def find_root(self, message_ids: list[str]) -> int | None:
        """Maps the Message-IDs a reply refers to onto the conversation they belong to."""
        if not message_ids:
            return None
        marks = ",".join("?" * len(message_ids))
        with closing(self._connect()) as db:
            row = db.execute(
                f"SELECT COALESCE(parent_id, id) FROM tasks WHERE message_id IN ({marks})"
                f" UNION ALL SELECT root_id FROM sent WHERE message_id IN ({marks}) LIMIT 1",
                (*message_ids, *message_ids),
            ).fetchone()
        return row[0] if row else None
