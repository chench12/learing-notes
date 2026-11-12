"""SQLite 是记录事实来源；JSON/源码/日志是可查看的文件导出。

先持久化内容寻址文件，再在事务中提交记录、事件和待导出清单。
提交后中断时，下次打开只补齐待导出文件，不从不完整目录猜测事实。
"""

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import threading
from uuid import uuid4


KINDS = {"candidate", "generation", "evaluation", "group", "archive", "call", "control", "meta"}


def encode_json(value):
    return json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def atomic_bytes(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


class Storage:
    def __init__(self, directory, experiment_id, fingerprint, *, create=False):
        self.directory = Path(directory).resolve()
        self.path = self.directory / "records.sqlite3"
        self._mutex = threading.RLock()
        self._depth = 0
        self._lock_stream = None
        if not create and not self.path.is_file():
            raise ValueError("实验缺少 records.sqlite3；不从 JSON 静默重建，请保留/恢复原数据库")
        with self.operation(), self.connection() as connection:
            if create:
                connection.executescript("""
                    CREATE TABLE identity (experiment_id TEXT NOT NULL, fingerprint TEXT NOT NULL, version INTEGER NOT NULL);
                    CREATE TABLE records (
                        kind TEXT NOT NULL, id TEXT NOT NULL, payload TEXT NOT NULL,
                        state TEXT, candidate_id TEXT, revision INTEGER NOT NULL,
                        PRIMARY KEY(kind, id)
                    );
                    CREATE INDEX records_kind_state ON records(kind, state);
                    CREATE INDEX records_candidate ON records(candidate_id, kind);
                    CREATE TABLE files (
                        path TEXT PRIMARY KEY, kind TEXT NOT NULL, record_id TEXT NOT NULL,
                        sha256 TEXT NOT NULL, size INTEGER NOT NULL, pending INTEGER NOT NULL,
                        FOREIGN KEY(kind, record_id) REFERENCES records(kind, id)
                    );
                    CREATE TABLE events (
                        sequence INTEGER PRIMARY KEY AUTOINCREMENT, created_at_utc TEXT NOT NULL,
                        kind TEXT NOT NULL, record_id TEXT NOT NULL, revision INTEGER NOT NULL,
                        state TEXT, payload TEXT NOT NULL, file_changes TEXT NOT NULL,
                        UNIQUE(kind, record_id, revision)
                    );
                """)
                connection.execute("INSERT INTO identity VALUES (?, ?, 1)", (experiment_id, fingerprint))
                connection.commit()
            identity = connection.execute("SELECT * FROM identity").fetchall()
            if len(identity) != 1 or tuple(identity[0]) != (experiment_id, fingerprint, 1):
                raise ValueError("SQLite 实验身份与清单不一致")

    @contextmanager
    def connection(self):
        connection = sqlite3.connect(self.path, timeout=5)
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA synchronous=FULL")
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def operation(self):
        """单实验单操作；OS 锁随进程退出释放，不以残留锁文件判断占用。"""
        with self._mutex:
            if self._depth == 0:
                stream = (self.directory / ".operation.lock").open("a+b")
                try:
                    if stream.seek(0, 2) == 0:
                        stream.write(b"0")
                        stream.flush()
                    stream.seek(0)
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    stream.close()
                    raise RuntimeError("此实验正被另一操作使用；请勿并发生成、评测或恢复") from None
                self._lock_stream = stream
            self._depth += 1
            try:
                yield
            finally:
                self._depth -= 1
                if self._depth == 0:
                    stream = self._lock_stream
                    stream.seek(0)
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(stream, fcntl.LOCK_UN)
                    stream.close()
                    self._lock_stream = None

    def _target(self, name):
        relative = PurePosixPath(name)
        if (not name or relative.is_absolute() or ".." in relative.parts
                or "\\" in name or ":" in name or "\x00" in name
                or relative.as_posix() != name
                or relative.parts[0] not in ("candidates", "generations", "evaluations", "evaluation_groups", "archives", "calls", "controls", "meta_prompts")):
            raise ValueError("记录文件必须位于实验的数据目录内")
        target = self.directory.joinpath(*relative.parts)
        if not target.resolve().is_relative_to(self.directory):
            raise ValueError("记录文件不能通过链接越过实验目录")
        return target

    def _blob(self, digest):
        return self.directory / ".objects" / digest[:2] / digest

    def put(self, kind, identifier, payload, files):
        if kind not in KINDS or not re.fullmatch(r"[A-Za-z0-9_-]+", identifier):
            raise ValueError("未知记录种类或非法记录编号")
        encoded = encode_json(payload).decode("utf-8")
        changes = {}
        with self.operation():
            for name, data in files.items():
                self._target(name)
                digest = sha256(data)
                blob = self._blob(digest)
                if blob.exists():
                    if sha256(blob.read_bytes()) != digest:
                        raise ValueError("持久化内容文件哈希不一致")
                else:
                    atomic_bytes(blob, data)
                changes[name] = (digest, len(data))
            with self.connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                old = connection.execute("SELECT payload, revision FROM records WHERE kind=? AND id=?", (kind, identifier)).fetchone()
                changed = old is None or old[0] != encoded
                for name, (digest, _) in changes.items():
                    previous = connection.execute("SELECT kind, record_id, sha256 FROM files WHERE path=?", (name,)).fetchone()
                    if previous and previous[:2] != (kind, identifier):
                        raise ValueError("文件不能被另一个记录接管")
                    changed = changed or previous is None or previous[2] != digest
                if changed:
                    revision = old[1] + 1 if old else 1
                    state = payload.get("state")
                    if isinstance(state, dict):
                        state = state.get("status")
                    state = state if isinstance(state, str) else "recorded"
                    candidate_id = (payload.get("candidate_id") or payload.get("plan", {}).get("child_candidate_id")
                                    or payload.get("plan", {}).get("candidate_id")
                                    or payload.get("candidate", {}).get("candidate_id"))
                    connection.execute("""
                        INSERT INTO records VALUES (?, ?, ?, ?, ?, ?)
                        ON CONFLICT(kind,id) DO UPDATE SET payload=excluded.payload,
                        state=excluded.state, candidate_id=excluded.candidate_id, revision=excluded.revision
                    """, (kind, identifier, encoded, state, candidate_id, revision))
                    for name, (digest, size) in changes.items():
                        connection.execute("""
                            INSERT INTO files VALUES (?, ?, ?, ?, ?, 1)
                            ON CONFLICT(path) DO UPDATE SET sha256=excluded.sha256, size=excluded.size,
                            pending=CASE WHEN files.sha256=excluded.sha256 THEN files.pending ELSE 1 END
                        """, (name, kind, identifier, digest, size))
                    connection.execute("INSERT INTO events(created_at_utc,kind,record_id,revision,state,payload,file_changes) VALUES (?,?,?,?,?,?,?)", (
                        datetime.now(timezone.utc).isoformat(), kind, identifier, revision, state,
                        encoded, json.dumps({name: digest for name, (digest, _) in changes.items()}),
                    ))
                connection.commit()
            self.publish_pending()

    def publish_pending(self):
        with self.operation(), self.connection() as connection:
            rows = connection.execute("SELECT path,sha256,size FROM files WHERE pending=1 ORDER BY path").fetchall()
            for name, digest, size in rows:
                data = self._blob(digest).read_bytes()
                if len(data) != size or sha256(data) != digest:
                    raise ValueError(f"持久化文件损坏：{name}")
                target = self._target(name)
                if not target.is_file() or sha256(target.read_bytes()) != digest:
                    atomic_bytes(target, data)
                connection.execute("UPDATE files SET pending=0 WHERE path=? AND sha256=?", (name, digest))
                connection.commit()

    def verify_files(self):
        with self.operation(), self.connection() as connection:
            for name, digest, size in connection.execute("SELECT path,sha256,size FROM files"):
                target = self._target(name)
                if not target.is_file() or target.stat().st_size != size or sha256(target.read_bytes()) != digest:
                    raise ValueError(f"记录导出文件被修改或缺失：{name}")

    def get(self, kind, identifier):
        with self.connection() as connection:
            row = connection.execute("SELECT payload FROM records WHERE kind=? AND id=?", (kind, identifier)).fetchone()
        return json.loads(row[0]) if row else None

    def all(self, kind):
        with self.connection() as connection:
            rows = connection.execute("SELECT id,payload FROM records WHERE kind=? ORDER BY id", (kind,)).fetchall()
        return [(identifier, json.loads(payload)) for identifier, payload in rows]

    def events(self):
        """按事务提交顺序读取事件，用于确定性重放档案。"""
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT sequence,kind,record_id,revision,state,payload FROM events ORDER BY sequence"
            ).fetchall()
        return [{"sequence": row[0], "kind": row[1], "record_id": row[2],
                 "revision": row[3], "state": row[4], "payload": json.loads(row[5])}
                for row in rows]

    def stats(self):
        with self.connection() as connection:
            counts = dict(connection.execute("SELECT kind,count(*) FROM records GROUP BY kind"))
            counts["events"] = connection.execute("SELECT count(*) FROM events").fetchone()[0]
            counts["pending_exports"] = connection.execute("SELECT count(*) FROM files WHERE pending=1").fetchone()[0]
        return counts
