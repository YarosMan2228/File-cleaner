"""Папка «Ready for approval»: кандидаты на удаление ждут твоего решения.

Как это работает:
  • проверка переносит сюда файлы, разложенные по причинам («Дубликаты - имя-копия», …);
  • ты смотришь их в Проводнике; что нужно оставить — перетаскиваешь в «_ВЕРНУТЬ»
    (или просто забираешь куда угодно);
  • команда «утвердить» возвращает всё из «_ВЕРНУТЬ» на прежние места, а остальное удаляет.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import config, report
from .analyzers import Finding
from .fsutil import display, human_size, is_under, list_dir, long_path, path_is_link, plural, remove_file
from .journal import Session
from .winutil import fixed_drives

MANIFEST = ".manifest.json"
REPORT = "ОТЧЁТ.html"
HOWTO = "КАК ПОЛЬЗОВАТЬСЯ.txt"
_BAD_CHARS = str.maketrans({c: "_" for c in '<>:"/\\|?*'})

HOWTO_TEXT = """Здесь лежат файлы, которые программа считает ненужными. Пока ничего не удалено.

1. Открой ОТЧЁТ.html — там для каждого файла написано, откуда он и почему попал сюда.
2. Что нужно оставить — перетащи в папку «{ret}» (можно целыми папками).
   Или просто забери файл куда тебе нужно.
3. Запусти:  filecleaner approve  (или пункт «Утвердить удаление» в меню).
   Всё из «{ret}» вернётся на прежние места, остальное удалится насовсем.

Передумал целиком? filecleaner undo — всё вернётся на свои места.
"""


@dataclass
class Batch:
    path: Path
    created: datetime
    entries: list[dict] = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.path.name

    def present(self) -> list[dict]:
        """Записи, файлы которых всё ещё лежат в папке проверки."""
        return [e for e in self.entries if os.path.lexists(long_path(self.path / e["staged"]))]

    def size(self) -> int:
        return sum(e.get("size", 0) for e in self.present())


def anchor(path: Path) -> Path:
    """Путь внутри папки проверки: «Downloads\\x.pdf» или «B\\Архив\\x.pdf» — чтобы было видно, откуда файл."""
    if is_under(path, config.HOME):
        return Path(str(path)[len(str(config.HOME)):].lstrip("\\/"))
    drive = path.drive.rstrip(":").replace("\\", "") or "_"
    return Path(drive, *path.parts[1:])


def _safe(name: str) -> str:
    return name.translate(_BAD_CHARS).strip() or "Прочее"


def stage(findings: list[Finding], session: Session, title: str = "проверка") -> tuple[list[Batch], list[str]]:
    """Переносит найденное в «Ready for approval» (на каждом диске — своя папка) и пишет отчёт."""
    stamp = datetime.now()
    batches: dict[str, Batch] = {}
    errors: list[str] = []
    for finding in findings:
        root = config.review_root(finding.path)
        batch = batches.get(str(root))
        if batch is None:
            folder = root / f"{stamp:%Y-%m-%d %H-%M} {title}"
            n = 1
            while folder.exists():
                n += 1
                folder = root / f"{stamp:%Y-%m-%d %H-%M} {title} ({n})"
            batch = batches[str(root)] = Batch(folder, stamp)
        target = batch.path / _safe(finding.group) / anchor(finding.path)
        try:
            final = session.move(finding.path, target, op="stage", rule=finding.rule)
        except OSError as exc:
            errors.append(f"{display(finding.path)}: {exc.strerror or exc}")
            continue
        batch.entries.append({
            "staged": str(final.relative_to(batch.path)),
            "original": str(finding.path),
            "size": finding.size,
            "dir": finding.is_dir,
            "rule": finding.rule,
            "group": finding.group,
            "reason": finding.reason,
            "keep": str(finding.original) if finding.original else "",
        })
    for batch in batches.values():
        if not batch.entries:
            continue
        (batch.path / config.RETURN_DIR_NAME).mkdir(exist_ok=True)
        write_manifest(batch)
        (batch.path / HOWTO).write_text(HOWTO_TEXT.format(ret=config.RETURN_DIR_NAME), encoding="utf-8-sig")
        (batch.path / REPORT).write_text(batch_report(batch), encoding="utf-8")
        session.record("batch", path=str(batch.path))
    return [b for b in batches.values() if b.entries], errors


def write_manifest(batch: Batch) -> None:
    data = {"created": batch.created.isoformat(timespec="seconds"), "entries": batch.entries}
    (batch.path / MANIFEST).write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def batch_report(batch: Batch) -> str:
    groups: dict[str, list[dict]] = {}
    for entry in batch.entries:
        groups.setdefault(entry["group"], []).append(entry)
    sections = []
    for group, entries in sorted(groups.items(), key=lambda kv: -sum(e["size"] for e in kv[1])):
        rows = [[e["staged"].split(os.sep, 1)[-1], display(e["original"]), human_size(e["size"]), e["reason"]]
                for e in sorted(entries, key=lambda e: -e["size"])]
        sections.append(report.Section(
            f"{group} — {human_size(sum(e['size'] for e in entries))}",
            ["Файл", "Откуда", "Размер", "Почему здесь"], rows, note=plural(len(entries), "объект", "объекта", "объектов"),
        ))
    total = sum(e["size"] for e in batch.entries)
    return report.render(
        f"На проверку: {batch.name}",
        f"Ничего не удалено. Нужное перетащи в «{config.RETURN_DIR_NAME}», потом запусти «filecleaner approve».",
        [("Объектов", str(len(batch.entries))), ("Занимают", human_size(total))],
        sections,
    )


def remove_batch_files(folder: Path) -> None:
    """Убирает служебные файлы партии (используется при отмене)."""
    for name in (MANIFEST, REPORT, HOWTO):
        try:
            remove_file(folder / name)
        except OSError:
            pass
    for path in (folder / config.RETURN_DIR_NAME, folder):
        try:
            os.rmdir(long_path(path))
        except OSError:
            pass


# -------------------------------------------------------------------- поиск партий
def review_roots() -> list[Path]:
    roots = [config.REVIEW_HOME]
    for drive in fixed_drives():
        candidate = drive / config.REVIEW_DIR_NAME
        if candidate not in roots:
            roots.append(candidate)
    return roots


def load_batch(folder: Path) -> Batch | None:
    try:
        data = json.loads((folder / MANIFEST).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return Batch(folder, datetime.fromisoformat(data["created"]), data.get("entries", []))


def find_batches() -> list[Batch]:
    batches = []
    for root in review_roots():
        for entry in list_dir(str(root)) or []:
            if entry.is_dir():
                batch = load_batch(Path(entry.path))
                if batch is not None:
                    batches.append(batch)
    return sorted(batches, key=lambda b: b.created)


# -------------------------------------------------------------------- утверждение
@dataclass
class ApproveResult:
    deleted: int = 0
    freed: int = 0
    restored: list[str] = field(default_factory=list)
    kept_outside: int = 0            # ты сам забрал из папки — не трогаем
    unmatched: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _match_returned(batch: Batch, taken: set[int]) -> tuple[list[tuple[Path, dict]], list[Path]]:
    """Сопоставляет содержимое «_ВЕРНУТЬ» с записями партии: по имени, пути и размеру."""
    ret_dir = batch.path / config.RETURN_DIR_NAME
    matches: list[tuple[Path, dict]] = []
    unmatched: list[Path] = []

    def candidates(rel: Path, is_dir: bool, size: int | None) -> list[int]:
        rel_parts = [p.lower() for p in rel.parts]
        found = []
        for i, entry in enumerate(batch.entries):
            if i in taken or bool(entry.get("dir")) != is_dir:
                continue
            staged = [p.lower() for p in Path(entry["staged"]).parts]
            if staged[-len(rel_parts):] != rel_parts:
                continue
            if size is not None and entry.get("size") != size:
                continue
            found.append(i)
        return found

    def visit(item: Path) -> None:
        rel = item.relative_to(ret_dir)
        is_dir = item.is_dir() and not path_is_link(item)
        size = None if is_dir else item.stat().st_size
        found = candidates(rel, is_dir, size)
        if len(found) == 1:
            taken.add(found[0])
            matches.append((item, batch.entries[found[0]]))
        elif is_dir:
            for child in sorted(item.iterdir()):
                visit(child)
        else:
            unmatched.append(item)

    if ret_dir.is_dir():
        for item in sorted(ret_dir.iterdir()):
            visit(item)
    return matches, unmatched


def approve(batch: Batch, session: Session) -> ApproveResult:
    result = ApproveResult()
    returned: set[int] = set()
    matches, unmatched = _match_returned(batch, returned)
    for item, entry in matches:
        try:
            final = session.move(item, Path(entry["original"]), op="move", restored=True)
            result.restored.append(display(final))
        except OSError as exc:
            result.errors.append(f"не вернул {item.name}: {exc.strerror or exc}")
    result.unmatched = [display(p) for p in unmatched]

    failed: list[dict] = []
    for i, entry in enumerate(batch.entries):
        if i in returned:
            continue
        staged = batch.path / entry["staged"]
        if not os.path.lexists(long_path(staged)):
            result.kept_outside += 1
            continue
        try:
            session.delete(staged, entry.get("size", 0), is_dir=bool(entry.get("dir")), original=entry["original"])
            result.deleted += 1
            result.freed += entry.get("size", 0)
        except OSError as exc:
            failed.append(entry)
            result.errors.append(f"не удалил {display(staged)}: {exc.strerror or exc}")

    _cleanup(batch, failed)
    return result


def resolve(batch: Batch, session: Session, restore: set[str], delete: set[str]) -> ApproveResult:
    """Решение по отдельным объектам — без перетаскивания в «_ВЕРНУТЬ» (для окна программы).

    Объекты задаются путём внутри партии (entry["staged"]): restore — вернуть на место, delete — удалить.
    Остальное ждёт дальше.
    """
    result = ApproveResult()
    waiting: list[dict] = []
    for entry in batch.entries:
        key = entry["staged"]
        if key not in restore and key not in delete:
            waiting.append(entry)
            continue
        staged = batch.path / key
        if not os.path.lexists(long_path(staged)):
            result.kept_outside += 1
            continue
        try:
            if key in restore:
                final = session.move(staged, Path(entry["original"]), op="move", restored=True)
                result.restored.append(display(final))
            else:
                session.delete(staged, entry.get("size", 0), is_dir=bool(entry.get("dir")), original=entry["original"])
                result.deleted += 1
                result.freed += entry.get("size", 0)
        except OSError as exc:
            waiting.append(entry)
            verb = "не вернул" if key in restore else "не удалил"
            result.errors.append(f"{verb} {display(staged)}: {exc.strerror or exc}")
    _cleanup(batch, waiting)
    if waiting:  # партия ещё ждёт: отчёт и «_ВЕРНУТЬ» — по оставшемуся
        (batch.path / config.RETURN_DIR_NAME).mkdir(exist_ok=True)
        (batch.path / REPORT).write_text(batch_report(batch), encoding="utf-8")
    return result


def _cleanup(batch: Batch, failed: list[dict]) -> None:
    """Убирает опустевшие папки. Неудалённое остаётся в партии, чужие файлы — не трогаем."""
    root = long_path(batch.path)
    for dirpath, _, _ in os.walk(root, topdown=False):
        if os.path.normcase(dirpath) == os.path.normcase(root):
            continue
        try:
            if not os.listdir(dirpath):
                os.rmdir(dirpath)
        except OSError:
            pass
    if failed:  # партия остаётся — можно утвердить ещё раз
        batch.entries = failed
        write_manifest(batch)
        return
    leftovers = [e.name for e in list_dir(str(batch.path)) or []]
    if any(name not in (MANIFEST, REPORT, HOWTO) for name in leftovers):
        (batch.path / MANIFEST).unlink(missing_ok=True)  # партия закрыта, остались только твои файлы
        return
    remove_batch_files(batch.path)
