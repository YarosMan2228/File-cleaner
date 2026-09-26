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
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import config, report, verify
from .analyzers import Finding
from .i18n import tr
from .fsutil import display, human_size, is_under, list_dir, long_path, path_is_link, plural, remove_file
from .journal import Session
from .winutil import fixed_drives

MANIFEST = ".manifest.json"
REPORT = "ОТЧЁТ.html"
HOWTO = "КАК ПОЛЬЗОВАТЬСЯ.txt"
# Служебные имена на всех языках: партию, созданную по-русски, можно закрыть и по-английски.
REPORT_NAMES = (REPORT, "REPORT.html")
HOWTO_NAMES = (HOWTO, "HOW TO USE.txt")
RETURN_NAMES = (config.RETURN_DIR_NAME, "_RETURN")
_BAD_CHARS = str.maketrans({c: "_" for c in '<>:"/\\|?*'})

HOWTO_TEXT = (
    "Здесь лежат файлы, которые программа считает ненужными. Пока ничего не удалено.\n\n"
    "Проще всего — открой File Cleaner, вкладка «На решение»: отметь, что удалить, а что вернуть на место.\n\n"
    "Или вручную:\n"
    "1. Открой {report} — там для каждого файла написано, откуда он и почему попал сюда.\n"
    "2. Что нужно оставить — перетащи в папку «{ret}» (можно целыми папками).\n"
    "3. Запусти:  filecleaner approve — всё из «{ret}» вернётся на прежние места, остальное удалится насовсем.\n\n"
    "Передумал целиком? filecleaner undo — всё вернётся на свои места.\n"
)


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
    return name.translate(_BAD_CHARS).strip() or tr("Прочее")


def stage(findings: list[Finding], session: Session, title: str | None = None) -> tuple[list[Batch], list[str]]:
    """Переносит найденное в «Ready for approval» (на каждом диске — своя папка) и пишет отчёт."""
    stamp = datetime.now()
    title = title or tr("проверка")
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
        (batch.path / return_dir_name()).mkdir(exist_ok=True)
        write_manifest(batch)
        (batch.path / howto_name()).write_text(tr(HOWTO_TEXT, report=report_name(), ret=return_dir_name()),
                                              encoding="utf-8-sig")
        (batch.path / report_name()).write_text(batch_report(batch), encoding="utf-8")
        session.record("batch", path=str(batch.path))
    return [b for b in batches.values() if b.entries], errors


def report_name() -> str:
    return tr(REPORT)


def howto_name() -> str:
    return tr(HOWTO)


def return_dir_name() -> str:
    return tr(config.RETURN_DIR_NAME)


def write_manifest(batch: Batch) -> None:
    data = {"created": batch.created.isoformat(timespec="seconds"), "entries": batch.entries}
    (batch.path / MANIFEST).write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def verifiable(entry: dict) -> bool:
    """Можно ли сверить: архив — с папкой, куда его распаковали; копию — с оригиналом."""
    rule = str(entry.get("rule") or "")
    return bool(entry.get("keep")) and (rule == "archives.extracted" or rule.startswith("duplicates."))


def verdict(entry: dict) -> verify.Verdict | None:
    """Чем закончилась сверка, если её уже делали."""
    return verify.Verdict.from_dict(entry.get("verified"))


def check_entry(batch: Batch, entry: dict, progress: Callable[[str], None] = lambda text: None) -> verify.Verdict:
    """Сверяет объект партии и запоминает итог в записи (сохранить опись — write_manifest)."""
    staged, keep = batch.path / entry["staged"], Path(entry["keep"])
    if entry.get("rule") == "archives.extracted":
        result = verify.check_archive(staged, keep, progress)
    else:
        result = verify.check_copy(staged, keep)
    entry["verified"] = {**result.to_dict(), "at": datetime.now().isoformat(timespec="minutes")}
    return result


def check_batches(batches: list[Batch], progress: Callable[[str], None] = lambda text: None) -> dict[str, int]:
    """Сверяет в партиях всё, что можно сверить: сколько можно удалять, нельзя и не проверить."""
    counts = {"ok": 0, "bad": 0, "unknown": 0}
    for batch in batches:
        todo = [e for e in batch.present() if verifiable(e)]
        for entry in todo:
            ok = check_entry(batch, entry, progress).ok
            counts["ok" if ok else "bad" if ok is False else "unknown"] += 1
        if todo:
            write_manifest(batch)
    return counts


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
            [tr("Файл"), tr("Откуда"), tr("Размер"), tr("Почему здесь")], rows,
            note=plural(len(entries), "объект", "объекта", "объектов"),
        ))
    total = sum(e["size"] for e in batch.entries)
    return report.render(
        tr("На проверку: {name}", name=batch.name),
        tr("Ничего не удалено. Реши в окне File Cleaner («На решение») или перетащи нужное в «{ret}» "
           "и запусти «filecleaner approve».", ret=return_dir_name()),
        [(tr("Объектов"), str(len(batch.entries))), (tr("Занимают"), human_size(total))],
        sections,
    )


def remove_batch_files(folder: Path) -> None:
    """Убирает служебные файлы партии (используется при отмене)."""
    for name in (MANIFEST, *REPORT_NAMES, *HOWTO_NAMES):
        try:
            remove_file(folder / name)
        except OSError:
            pass
    for path in (*(folder / name for name in RETURN_NAMES), folder):
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
    ret_dir = next((batch.path / n for n in RETURN_NAMES if (batch.path / n).is_dir()), batch.path / RETURN_NAMES[0])
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
            result.errors.append(tr("не вернул {name}: {error}", name=item.name, error=exc.strerror or exc))
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
            result.errors.append(tr("не удалил {name}: {error}", name=display(staged), error=exc.strerror or exc))

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
            template = "не вернул {name}: {error}" if key in restore else "не удалил {name}: {error}"
            result.errors.append(tr(template, name=display(staged), error=exc.strerror or exc))
    _cleanup(batch, waiting)
    if waiting:  # партия ещё ждёт: отчёт и «_ВЕРНУТЬ» — по оставшемуся
        (batch.path / return_dir_name()).mkdir(exist_ok=True)
        (batch.path / report_name()).write_text(batch_report(batch), encoding="utf-8")
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
    if any(name not in (MANIFEST, *REPORT_NAMES, *HOWTO_NAMES) for name in leftovers):
        (batch.path / MANIFEST).unlink(missing_ok=True)  # партия закрыта, остались только твои файлы
        return
    remove_batch_files(batch.path)
