import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path


def now():
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(root / "history.sqlite3", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS tasks (
          id TEXT PRIMARY KEY, created TEXT NOT NULL, updated TEXT NOT NULL,
          status TEXT NOT NULL, repository TEXT NOT NULL, goal TEXT NOT NULL,
          payload TEXT NOT NULL, result TEXT NOT NULL DEFAULT '{}', error TEXT,
          usage TEXT NOT NULL DEFAULT '{"input":0,"output":0,"calls":0}'
        );
        CREATE TABLE IF NOT EXISTS events (
          id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
          time TEXT NOT NULL, role TEXT NOT NULL, kind TEXT NOT NULL,
          summary TEXT NOT NULL, details TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS events_task ON events(task_id,id);
        ''')
        self.db.commit()

    def create(self, payload):
        task_id = uuid.uuid4().hex
        timestamp = now()
        with self.lock, self.db:
            self.db.execute("INSERT INTO tasks(id,created,updated,status,repository,goal,payload) VALUES(?,?,?,?,?,?,?)",
                            (task_id, timestamp, timestamp, "queued", payload["repository"], payload["goal"], json.dumps(payload)))
        return self.get(task_id)

    def get(self, task_id):
        with self.lock:
            row = self.db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise KeyError(task_id)
        data = dict(row)
        for field in ("payload", "result", "usage"):
            data[field] = json.loads(data[field])
        return data

    def list(self):
        with self.lock:
            return [dict(row) for row in self.db.execute("SELECT id,created,updated,status,repository,goal,error FROM tasks ORDER BY created DESC LIMIT 200")]

    def update(self, task_id, **fields):
        allowed = {"status", "result", "error", "usage"}
        if not set(fields) <= allowed:
            raise ValueError("Invalid field")
        fields["updated"] = now()
        values = [json.dumps(v, ensure_ascii=False) if isinstance(v, dict) else v for v in fields.values()]
        with self.lock, self.db:
            self.db.execute(f"UPDATE tasks SET {','.join(f'{k}=?' for k in fields)} WHERE id=?", (*values, task_id))

    def event(self, task_id, role, kind, summary, details=None):
        with self.lock, self.db:
            details=dict(details or {})
            previous=self.db.execute("SELECT id,details,time FROM events WHERE task_id=? AND role=? AND kind='started' ORDER BY id DESC LIMIT 1",(task_id,role)).fetchone()
            details.setdefault('task_id',task_id)
            details.setdefault('step_id',uuid.uuid4().hex if kind=='started' or not previous else json.loads(previous['details']).get('step_id',str(previous['id'])))
            if previous and kind in {'completed','failed','rejected'}:
                details.setdefault('duration_ms',round((datetime.now(timezone.utc)-datetime.fromisoformat(previous['time'])).total_seconds()*1000))
            cur = self.db.execute("INSERT INTO events(task_id,time,role,kind,summary,details) VALUES(?,?,?,?,?,?)",
                                  (task_id, now(), role, kind, summary, json.dumps(details or {}, ensure_ascii=False)))
            return cur.lastrowid

    def events(self, task_id, after=0):
        with self.lock:
            rows = self.db.execute("SELECT * FROM events WHERE task_id=? AND id>? ORDER BY id LIMIT 1000", (task_id, after)).fetchall()
        return [{**dict(row), "details": json.loads(row["details"])} for row in rows]

    def workspace(self, task_id):
        if len(task_id) != 32 or any(c not in "0123456789abcdef" for c in task_id):
            raise ValueError("Invalid task ID")
        path = self.root / "tasks" / task_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def save_json(self, task_id, name, data):
        path = self.workspace(task_id) / name
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(path)

    def recover(self):
        # Recovery must include older tasks, not just the 200-row sidebar page.
        for task in self.db.execute('SELECT id,status FROM tasks').fetchall():
            if task["status"] in {"queued", "planning", "searching", "reading", "analyzing", "teaching", "drawing", "reviewing", "repairing", "stopping"}:
                self.update(task["id"], status="interrupted", error="上次运行意外中断，可重新发起。")
                self.event(task["id"], "System", "interrupted", "检测到上次运行中断，已有成果已保留")
