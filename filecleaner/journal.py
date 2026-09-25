"""Журнал действий: всё, что программа сделала с твоими файлами, можно отменить."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import config
from .i18n import tr
from .fsutil import (
    PartialMoveError, long_path, move_path, path_is_link, remove_file, remove_tree, unique_path,
)

# Эти действия отмена умеет откатывать; удаление — нет.
UNDOABLE_OPS = {"move", "stage", "mkdir", "rmdir", "link", "reg_set", "lnk_set", "compress", "batch"}
NOT_UNDOABLE_KINDS = {"approve"}


def journal_dir() -> Path:
    return config.DATA_DIR / "journal"


class Session:
    """Одна операция (проверка, сортировка, сжатие…). Журнал пишется построчно — сбой не потеряет историю."""

    def __init__(self, kind: str, title: str) -> None:
        self.kind = kind
        self.title = title
        self.created = datetime.now()
        base = f"{self.created:%Y%m%d-%H%M%S}-{kind}"
        self.id = base
        n = 1
        while (journal_dir() / f"{self.id}.jsonl").exists():
            n += 1
            self.id = f"{base}-{n}"
        self.path = journal_dir() / f"{self.id}.jsonl"
        self._fh = None

    def __enter__(self) -> "Session":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None

    def _write(self, record: dict) -> None:
        if self._fh is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = open(self.path, "a", encoding="utf-8")
            header = {"type": "session", "id": self.id, "kind": self.kind, "title": self.title,
                      "created": self.created.isoformat(timespec="seconds")}
            self._fh.write(json.dumps(header, ensure_ascii=False) + "\n")
        self._fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._fh.flush()

    # ---------------------------------------------------------------- действия
    def mkdirs(self, folder: Path) -> None:
        """Создаёт недостающие папки и запоминает их, чтобы отмена могла их убрать."""
        missing = []
        current = Path(folder)
        while not os.path.exists(long_path(current)):
            missing.append(current)
            if current.parent == current:
                break
            current = current.parent
        for directory in reversed(missing):
            os.mkdir(long_path(directory))
            self._write({"op": "mkdir", "path": str(directory)})

    def move(self, src: Path, dst: Path, *, op: str = "move", **extra) -> Path:
        """Переносит файл/папку в dst (или «dst (2)», если занято) и возвращает итоговый путь."""
        dst = unique_path(Path(dst))
        self.mkdirs(dst.parent)
        is_dir = os.path.isdir(long_path(src)) and not path_is_link(src)
        record = {"op": op, "src": str(src), "dst": str(dst), "dir": is_dir, **extra}
        try:
            move_path(src, dst)
        except PartialMoveError:
            self._write({**record, "partial": True})
            raise
        self._write(record)
        return dst

    def delete(self, path: Path, size: int = 0, *, is_dir: bool = False, **extra) -> None:
        if is_dir:
            remove_tree(path)
        else:
            remove_file(path)
        self._write({"op": "delete", "path": str(path), "size": size, "dir": is_dir, **extra})

    def rmdir(self, path: Path) -> None:
        os.rmdir(long_path(path))
        self._write({"op": "rmdir", "path": str(path)})

    def record(self, op: str, **data) -> None:
        self._write({"op": op, **data})


# -------------------------------------------------------------------- чтение журнала
@dataclass
class SessionInfo:
    id: str
    kind: str
    title: str
    created: datetime
    path: Path
    entries: list[dict] = field(default_factory=list)
    marks: list[str] = field(default_factory=list)

    @property
    def undone(self) -> bool:
        return "undone" in self.marks

    def ops(self, *names: str) -> list[dict]:
        return [e for e in self.entries if e.get("op") in names]

    @property
    def undoable(self) -> int:
        if self.undone or self.kind in NOT_UNDOABLE_KINDS:
            return 0
        return len(self.ops("move", "stage", "rmdir", "link", "reg_set", "lnk_set", "compress"))

    def summary(self) -> str:
        from .fsutil import human_size, plural

        parts = []
        moved = self.ops("move")
        staged = self.ops("stage")
        deleted = self.ops("delete")
        if staged:
            parts.append(tr("на проверку {count}", count=plural(len(staged), "объект", "объекта", "объектов")))
        if moved:
            parts.append(tr("перенесено {count}", count=plural(len(moved), "объект", "объекта", "объектов")))
        if deleted:
            size = sum(e.get("size", 0) for e in deleted)
            parts.append(tr("удалено {count} ({size})", count=plural(len(deleted), "объект", "объекта", "объектов"),
                             size=human_size(size)))
        links = self.ops("link")
        if links:
            parts.append(tr("ссылок {count}", count=len(links)))
        compressed = self.ops("compress")
        if compressed:
            parts.append(tr("сжато {files}", files=plural(sum(len(e.get("paths", [])) for e in compressed),
                                                 "файл", "файла", "файлов")))
        return ", ".join(parts) or tr("без изменений")

    def mark(self, kind: str) -> None:
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"type": kind, "at": datetime.now().isoformat(timespec="seconds")}) + "\n")
        self.marks.append(kind)


def _load(path: Path) -> SessionInfo | None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    info = None
    for line in lines:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("type") == "session":
            info = SessionInfo(record["id"], record.get("kind", ""), record.get("title", ""),
                               datetime.fromisoformat(record["created"]), path)
        elif info is None:
            continue
        elif "op" in record:
            info.entries.append(record)
        elif "type" in record:
            info.marks.append(record["type"])
    return info


def list_sessions() -> list[SessionInfo]:
    folder = journal_dir()
    if not folder.exists():
        return []
    sessions = [s for s in (_load(p) for p in folder.glob("*.jsonl")) if s is not None]
    return sorted(sessions, key=lambda s: s.created, reverse=True)


def find_session(session_id: str) -> SessionInfo | None:
    return next((s for s in list_sessions() if s.id == session_id), None)


# -------------------------------------------------------------------- отмена
@dataclass
class UndoResult:
    restored: int = 0
    irreversible: int = 0
    missing: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def undo(info: SessionInfo) -> UndoResult:
    from . import compress, refs  # тут, чтобы не было циклического импорта

    result = UndoResult()
    shortcut_restores: list[dict] = []
    for entry in reversed(info.entries):
        op = entry.get("op")
        try:
            if op in ("move", "stage"):
                src, dst = Path(entry["src"]), Path(entry["dst"])
                if not os.path.lexists(long_path(dst)):
                    if not os.path.lexists(long_path(src)):
                        result.missing.append(str(dst))
                    continue  # либо уже на месте, либо пропал
                target = src if not os.path.lexists(long_path(src)) else unique_path(src)
                os.makedirs(long_path(target.parent), exist_ok=True)
                move_path(dst, target)
                result.restored += 1
            elif op == "mkdir":
                try:
                    os.rmdir(long_path(entry["path"]))
                except OSError:
                    pass  # в папке что-то есть — оставляем
            elif op == "rmdir":
                os.makedirs(long_path(entry["path"]), exist_ok=True)
                result.restored += 1
            elif op == "link":
                refs.remove_link(Path(entry["link"]), entry.get("dir", False))
            elif op == "reg_set":
                refs.restore_registry_value(entry)
                result.restored += 1
            elif op == "lnk_set":
                shortcut_restores.append(entry)
            elif op == "compress":
                compress.decompress([Path(p) for p in entry.get("paths", [])])
                result.restored += len(entry.get("paths", []))
            elif op == "batch":
                from .review import remove_batch_files
                remove_batch_files(Path(entry["path"]))
            elif op == "delete":
                result.irreversible += 1
        except OSError as exc:
            result.errors.append(f"{entry.get('src') or entry.get('path') or entry.get('link')}: {exc}")
    if shortcut_restores:
        result.restored += refs.restore_shortcuts(shortcut_restores)
    if not result.errors and not result.missing:
        info.mark("undone")
    return result
