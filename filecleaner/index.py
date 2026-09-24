"""Индекс файлов в SQLite: один проход по диску, хэши сохраняются между запусками."""
from __future__ import annotations

import hashlib
import os
import sqlite3
import time
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from . import config
from .fsutil import attributes, birth_time, human_size, is_cloud_only, long_path, walk
from .winutil import read_zone_source

HEAD_BYTES = 64 * 1024
CHUNK = 1024 * 1024
Progress = Callable[[str], None]


def _quiet(_: str) -> None:
    pass


SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    key TEXT PRIMARY KEY,
    path TEXT NOT NULL,
    root TEXT NOT NULL,
    size INTEGER NOT NULL,
    mtime REAL NOT NULL,
    ctime REAL NOT NULL,
    attrs INTEGER NOT NULL,
    cloud INTEGER NOT NULL,
    protected INTEGER NOT NULL,
    source TEXT,
    scan INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS files_root ON files(root);
CREATE TABLE IF NOT EXISTS hashes (
    key TEXT PRIMARY KEY,
    size INTEGER NOT NULL,
    mtime REAL NOT NULL,
    head TEXT,
    full TEXT
);
"""
_UPSERT = {
    "head": """INSERT INTO hashes(key, size, mtime, head, full) VALUES (?, ?, ?, ?, NULL)
               ON CONFLICT(key) DO UPDATE SET
                 full = CASE WHEN hashes.size = excluded.size AND hashes.mtime = excluded.mtime
                             THEN hashes.full ELSE NULL END,
                 head = excluded.head, size = excluded.size, mtime = excluded.mtime""",
    "full": """INSERT INTO hashes(key, size, mtime, head, full) VALUES (?, ?, ?, NULL, ?)
               ON CONFLICT(key) DO UPDATE SET
                 head = CASE WHEN hashes.size = excluded.size AND hashes.mtime = excluded.mtime
                             THEN hashes.head ELSE NULL END,
                 full = excluded.full, size = excluded.size, mtime = excluded.mtime""",
}


@dataclass(frozen=True)
class FileRec:
    key: str
    path: Path
    root: str
    size: int
    mtime: float
    ctime: float
    attrs: int
    cloud: bool
    protected: bool   # внутри программы или проекта — по одному файлу не трогаем
    source: str       # откуда скачан (адреса из Zone.Identifier)

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def ext(self) -> str:
        return self.path.suffix.lower().lstrip(".")


@dataclass
class ScanStats:
    files: int = 0
    bytes: int = 0
    cloud: int = 0
    seconds: float = 0.0


def file_digest(path: Path, size: int, kind: str) -> str | None:
    """head — первые и последние 64 КБ (маленькие файлы целиком), full — весь файл."""
    digest = hashlib.blake2b(digest_size=20)
    try:
        with open(long_path(path), "rb") as fh:
            if kind == "head" and size > 2 * HEAD_BYTES:
                digest.update(fh.read(HEAD_BYTES))
                fh.seek(size - HEAD_BYTES)
                digest.update(fh.read(HEAD_BYTES))
            else:
                while chunk := fh.read(CHUNK):
                    digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


class Index:
    def __init__(self, db_path: Path | None = None) -> None:
        self.path = db_path or config.DATA_DIR / "index.db"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.executescript(SCHEMA)

    def __enter__(self) -> "Index":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        self.db.close()

    # ---------------------------------------------------------------- скан
    def scan(self, roots: dict[str, Path], progress: Progress = _quiet,
             sources_for: Iterable[str] = ("downloads", "desktop")) -> ScanStats:
        """Обновляет индекс для roots. Неизменившиеся файлы не перечитываются."""
        started = time.time()
        scan_id = time.time_ns()
        sources_for = set(sources_for)
        previous = {row[0]: row[1:] for row in self.db.execute("SELECT key, size, mtime, source FROM files")}
        seen: set[str] = set()
        stats = ScanStats()
        rows: list[tuple] = []

        def flush() -> None:
            self.db.executemany("INSERT OR REPLACE INTO files VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
            rows.clear()

        for name, root in roots.items():
            for entry, protected in walk(Path(root)):
                key = os.path.normcase(entry.path)
                if key in seen:
                    continue
                try:
                    st = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                seen.add(key)
                cloud = is_cloud_only(st)
                old = previous.get(key)
                if old and old[0] == st.st_size and old[1] == st.st_mtime:
                    source = old[2] or ""
                elif name in sources_for and not cloud:
                    source = read_zone_source(entry.path)
                else:
                    source = ""
                rows.append((key, entry.path, name, st.st_size, st.st_mtime, birth_time(st),
                             attributes(st), int(cloud), int(protected), source, scan_id))
                stats.files += 1
                stats.bytes += st.st_size
                stats.cloud += cloud
                if len(rows) >= 5000:
                    flush()
                if stats.files % 1000 == 0:
                    progress(f"Скан: {stats.files:,} файлов, {human_size(stats.bytes)}".replace(",", " "))
        flush()
        if roots:
            names = list(roots)
            marks = ",".join("?" * len(names))
            self.db.execute(f"DELETE FROM files WHERE scan != ? AND root IN ({marks})", (scan_id, *names))
        self.db.execute("DELETE FROM hashes WHERE key NOT IN (SELECT key FROM files)")
        self.db.commit()
        stats.seconds = time.time() - started
        return stats

    def files(self, roots: Iterable[str] | None = None) -> list[FileRec]:
        sql = "SELECT key, path, root, size, mtime, ctime, attrs, cloud, protected, source FROM files"
        params: list[str] = []
        if roots is not None:
            params = list(roots)
            if not params:
                return []
            sql += f" WHERE root IN ({','.join('?' * len(params))})"
        return [
            FileRec(key, Path(path), root, size, mtime, ctime, attrs, bool(cloud), bool(prot), source or "")
            for key, path, root, size, mtime, ctime, attrs, cloud, prot, source in self.db.execute(sql, params)
        ]

    # ---------------------------------------------------------------- хэши
    def hashes(self, recs: list[FileRec], kind: str, progress: Progress = _quiet) -> dict[str, str]:
        """Хэши для recs: из кэша, если файл не менялся, иначе считаются заново (в 4 потока)."""
        column = 3 if kind == "head" else 4
        cache = {row[0]: row for row in self.db.execute("SELECT key, size, mtime, head, full FROM hashes")}
        result: dict[str, str] = {}
        todo: list[FileRec] = []
        for rec in recs:
            row = cache.get(rec.key)
            if row and row[1] == rec.size and row[2] == rec.mtime and row[column]:
                result[rec.key] = row[column]
            else:
                todo.append(rec)
        if not todo:
            return result
        total = sum(min(r.size, 2 * HEAD_BYTES) if kind == "head" else r.size for r in todo)
        done = 0
        label = "Сравниваю начала файлов" if kind == "head" else "Сравниваю содержимое"
        with ThreadPoolExecutor(max_workers=4) as pool:
            digests = pool.map(lambda r: file_digest(r.path, r.size, kind), todo)
            for i, (rec, digest) in enumerate(zip(todo, digests), 1):
                done += min(rec.size, 2 * HEAD_BYTES) if kind == "head" else rec.size
                if digest is not None:
                    result[rec.key] = digest
                    self.db.execute(_UPSERT[kind], (rec.key, rec.size, rec.mtime, digest))
                if i % 50 == 0 or kind == "full":
                    progress(f"{label}: {i}/{len(todo)} ({human_size(done)} из {human_size(total)})")
        self.db.commit()
        return result
