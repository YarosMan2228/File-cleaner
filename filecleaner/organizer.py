"""Сортировка: файлы и папки раскладываются по секторам (Учёба, Работа…) и типам (Документы, Видео…)."""
from __future__ import annotations

import os
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from . import config, refs, review
from .ai import LocalAI, text_snippet
from .analyzers import Finding, protection
from .fsutil import (
    FILE_ATTRIBUTE_HIDDEN, FILE_ATTRIBUTE_SYSTEM, PartialMoveError, attributes, display, is_cloud_only,
    is_link, key_of, keyword_match, list_dir, long_path, looks_like_program_folder, name_tokens, unique_path, walk,
)
from .i18n import all_type_names, tr, type_name
from .journal import Session
from .rules import Rules
from .verify import same_file
from .winutil import read_zone_source

Progress = Callable[[str], None]
SETUP_NAMES = {"setup.exe", "install.exe", "installer.exe", "autorun.inf"}
# Какие файлы показывать ИИ, если правила не нашли сектор (у остальных смысл понятен из типа).
AI_TYPES = {"Документы", "Таблицы", "Презентации", "Книги", None}
PROTECTED = "не трогать: [protect] paths и keep_keywords"


def _quiet(_: str) -> None:
    pass


@dataclass
class SortMove:
    src: Path
    dst: Path
    is_dir: bool
    size: int
    label: str                       # «Учёба / Документы»
    reason: str                      # почему именно сюда
    duplicate_of: Path | None = None # в папке назначения уже лежит такой же файл
    referenced_by: set[str] = field(default_factory=set)


@dataclass
class SortPlan:
    folder: Path
    moves: list[SortMove]
    skipped: Counter
    unknown: list[Path]


@dataclass
class SortResult:
    moved: int = 0
    links: int = 0
    office: int = 0
    shortcuts: int = 0
    duplicates_staged: int = 0
    errors: list[str] = field(default_factory=list)


# ======================================================================= классификация
def file_type(name: str) -> tuple[str | None, str | None]:
    """(тип, подпапка по смыслу): «Screenshot 2024.png» → («Изображения», «Скриншоты»)."""
    path = Path(name)
    kind = config.EXT_TO_TYPE.get(path.suffix.lower().lstrip("."))
    if kind is None:
        return None, None
    for type_name, sub, pattern in config.SUBTYPE_RULES:
        if type_name == kind and pattern.search(path.stem):
            return kind, sub
    return kind, None


def domains(source: str) -> list[str]:
    out = []
    for url in source.split():
        try:
            host = urlsplit(url).hostname
        except ValueError:
            continue
        if host:
            out.append(host.lower().removeprefix("www."))
    return out


def match_sector(tokens: list[str], source_domains: list[str], kind: str | None,
                 sectors: list[dict]) -> tuple[dict | None, str]:
    """Сектор по сайту-источнику, потом по словам в имени, потом по типу файла."""
    for sector in sectors:
        for source in sector.get("sources", []):
            src = source.lower()
            for domain in source_domains:
                if domain == src or domain.endswith("." + src) or ("." not in src and src in domain):
                    return sector, tr("скачан с {domain}", domain=domain)
    for sector in sectors:
        for keyword in sector.get("keywords", []):
            if keyword_match(tokens, keyword):
                return sector, tr("в имени есть «{keyword}»", keyword=keyword)
    if kind:
        for sector in sectors:
            if kind in sector.get("types", []):
                return sector, tr("тип «{kind}»", kind=type_name(kind))
    return None, ""


def sector_dir(root: Path, sector: dict) -> Path:
    target = sector.get("target")
    return Path(target).expanduser() if target else root / sector["name"]


def _same_content(a: Path, b: Path) -> bool:
    return same_file(a, b)  # байт в байт, без кэша filecmp


# ======================================================================= план
def plan_sort(folder: Path, rules: Rules, progress: Progress = _quiet, now: float | None = None,
              check_references: bool = True, ai: LocalAI | None = None,
              move_folders: bool | None = None) -> SortPlan:
    """move_folders=None — как в правилах ([sort] move_folders); False — только отдельные файлы."""
    now = now or time.time()
    listing = list_dir(str(folder)) or []
    if looks_like_program_folder({e.name.lower() for e in listing}):
        return SortPlan(folder, [], Counter({tr("это папка программы или проекта — не раскладываю"): 1}), [])
    if move_folders is None:
        move_folders = bool(rules.get("sort.move_folders", True))
    sectors = rules.sectors
    ai = ai if ai is not None else LocalAI(rules)
    ai_budget = int(rules.get("ai.max_items", 500)) if sectors and ai.available() else 0

    def ask_ai(name: str, sources: list[str], snippet: str, key: str,
               kind: str | None = None) -> tuple[dict | None, str]:
        nonlocal ai_budget
        if ai_budget <= 0:
            return None, ""
        ai_budget -= 1
        progress(tr("ИИ смотрит: {name}", name=name))
        sector_name, why = ai.classify(name, sectors, sources, snippet, key, kind)
        sector = next((s for s in sectors if s["name"] == sector_name), None)
        return sector, tr("ИИ: {why}", why=why or tr("по содержимому")) if sector else ""

    min_age = float(rules.get("scan.min_file_age_minutes", 30)) * 60
    by_year = bool(rules.get("sort.by_year", False))
    unknown_to = str(rules.get("sort.unknown_to", "") or "")
    type_targets = rules.get("sort.type_targets", {}) or {}
    reserved = (
        all_type_names()  # папки типов на всех языках — сами они не раскладываются
        | {config.REVIEW_DIR_NAME.lower(), config.RETURN_DIR_NAME.lower(), "_return"}
        | {s["name"].lower() for s in sectors}
        | {str(name).lower() for name in rules.get("sort.keep_folders", []) or []}
    )
    if unknown_to:
        reserved.add(Path(unknown_to).name.lower())
    taken: set[str] = set()
    moves: list[SortMove] = []
    unknown: list[Path] = []
    skipped: Counter = Counter()
    protected_by = protection(rules, sorting=True)

    for entry in sorted(listing, key=lambda e: e.name.lower()):
        try:
            if is_link(entry):
                skipped[tr("ссылки")] += 1
                continue
            st = entry.stat(follow_symlinks=False)
            is_dir = entry.is_dir(follow_symlinks=False)
        except OSError:
            continue
        if attributes(st) & (FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM):
            skipped[tr("скрытые и системные")] += 1
            continue
        if protected_by(Path(entry.path)):
            skipped[tr(PROTECTED)] += 1
            continue
        if is_dir:
            if entry.name.lower() in reserved or not move_folders:
                continue
            progress(tr("Смотрю папку {name}…", name=entry.name))
            move = _plan_folder(Path(entry.path), folder, sectors, type_targets, now, min_age, taken, ask_ai,
                                protected_by)
            if isinstance(move, str):
                skipped[move] += 1
            elif move:
                moves.append(move)
            else:
                skipped[tr("папки: непонятно, куда их")] += 1
            continue

        name = entry.name
        low = name.lower()
        ext = Path(low).suffix.lstrip(".")
        if low in config.SERVICE_NAMES or ext in config.SHORTCUT_EXTS:
            skipped[tr("ярлыки и служебные файлы")] += 1
            continue
        if ext in config.PARTIAL_EXTS or ext == "tmp" or low.startswith("~$"):
            skipped[tr("недокачанные и временные")] += 1
            continue
        if now - st.st_mtime < min_age:
            skipped[tr("изменены только что")] += 1
            continue
        kind, sub = file_type(name)
        source = read_zone_source(entry.path)
        sector, why = match_sector(name_tokens(Path(name).stem), domains(source), kind, sectors)
        if sector is None and kind in AI_TYPES and not is_cloud_only(st):
            key = f"{key_of(entry.path)}|{st.st_size}|{st.st_mtime}"
            sector, why = ask_ai(name, domains(source), text_snippet(Path(entry.path)), key, kind)
        if sector:
            base = sector_dir(folder, sector) / type_name(kind)
            label = f"{sector['name']} / {type_name(kind)}"
        elif kind:
            target = type_targets.get(kind)
            base = Path(target).expanduser() if target else folder / type_name(kind)
            label, why = type_name(kind), tr("тип .{ext}", ext=ext)
        elif unknown_to:
            base = Path(unknown_to).expanduser()
            base = base if base.is_absolute() else folder / base
            label, why = unknown_to, tr("неизвестный тип")
        else:
            unknown.append(Path(entry.path))
            continue
        if sub:
            base, label = base / type_name(sub), f"{label} / {type_name(sub)}"
        if by_year:
            year = f"{datetime.fromtimestamp(st.st_mtime):%Y}"
            base, label = base / year, f"{label} / {year}"

        src = Path(entry.path)
        dst = base / name
        cloud = is_cloud_only(st)
        if cloud and dst.drive.upper() != src.drive.upper():
            skipped[tr("облачные файлы OneDrive (перенос на другой диск их скачает)")] += 1
            continue
        duplicate = None
        if os.path.lexists(long_path(dst)) and not cloud and _same_content(src, dst):
            duplicate = dst
        else:
            dst = unique_path(dst, taken)
            taken.add(key_of(dst))
        moves.append(SortMove(src, dst, False, st.st_size, label, why, duplicate))

    ai.save()
    if check_references and moves:
        found = refs.scan_references([folder], progress)
        for move in moves:
            move.referenced_by = refs.referenced_by(move.src, found, move.is_dir)
    return SortPlan(folder, moves, skipped, unknown)


def _plan_folder(path: Path, root: Path, sectors: list[dict], type_targets: dict, now: float,
                 min_age: float, taken: set[str], ask_ai: Callable | None = None,
                 protected_by: Callable[[Path], str | None] | None = None) -> SortMove | str | None:
    """Папку переносим целиком, только если понятно куда: по имени, по содержимому или это дистрибутив.

    Строка вместо переноса — причина не трогать папку (внутри защищённый файл).
    """
    by_type: Counter = Counter()
    total = count = 0
    newest = 0.0
    has_setup = False
    sample: list[str] = []
    for entry, _ in walk(path, rules=False):
        if protected_by is not None and protected_by(Path(entry.path)):
            return tr(PROTECTED)
        try:
            st = entry.stat(follow_symlinks=False)
        except OSError:
            continue
        count += 1
        total += st.st_size
        newest = max(newest, st.st_mtime)
        ext = Path(entry.name.lower()).suffix.lstrip(".")
        by_type[config.EXT_TO_TYPE.get(ext, config.OTHER_TYPE)] += st.st_size
        if entry.name.lower() in SETUP_NAMES or ext == "msi":
            has_setup = True
        if len(sample) < 20:
            sample.append(entry.name)
    if count == 0 or now - newest < min_age:
        return None
    kind = None
    if total:
        top, size = by_type.most_common(1)[0]
        if top != config.OTHER_TYPE and size / total >= 0.8:
            kind = top
    if kind is None and has_setup:
        kind = "Установщики"
    sector, why = match_sector(name_tokens(path.name), [], kind, sectors)
    if sector is None and kind is None and ask_ai is not None:
        sector, why = ask_ai(tr("{name} (папка)", name=path.name), [], tr("Файлы внутри: ") + ", ".join(sample),
                             f"{key_of(path)}|{count}|{total}|{newest}")
    if sector:
        base, label = sector_dir(root, sector), sector["name"]
    elif kind:
        target = type_targets.get(kind)
        base = Path(target).expanduser() if target else root / type_name(kind)
        label = type_name(kind)
        why = tr("дистрибутив программы") if kind == "Установщики" and has_setup \
            else tr("внутри в основном: {kind}", kind=type_name(kind).lower())
    else:
        return None
    dst = unique_path(base / path.name, taken)
    taken.add(key_of(dst))
    return SortMove(path, dst, True, total, tr("{label} (папка целиком)", label=label), why)


# ======================================================================= выполнение
def apply_sort(plan: SortPlan, rules: Rules, session: Session, progress: Progress = _quiet) -> SortResult:
    result = SortResult()
    moves = refs.MoveMap()
    duplicates: list[Finding] = []
    keep_links = bool(rules.get("links.keep_links_for_referenced", True))
    for i, move in enumerate(plan.moves, 1):
        progress(tr("Переношу {i}/{total}: {name}", i=i, total=len(plan.moves), name=move.src.name))
        if move.duplicate_of is not None:
            duplicates.append(Finding(
                "sort.duplicate", tr("Дубликаты - при сортировке"), move.src, move.size, "review",
                tr("в «{folder}» уже лежит точно такой же файл", folder=display(move.duplicate_of.parent)),
                original=move.duplicate_of,
            ))
            continue
        try:
            new = session.move(move.src, move.dst)
        except PartialMoveError as exc:
            result.errors.append(f"{display(move.src)}: {exc.strerror}")
            new = move.dst
        except OSError as exc:
            result.errors.append(f"{display(move.src)}: {exc.strerror or exc}")
            continue
        result.moved += 1
        moves.add(move.src, new, move.is_dir)
        if keep_links and refs.needs_link(move.referenced_by) and not os.path.lexists(long_path(move.src)):
            try:
                refs.leave_link(move.src, new, move.is_dir, session)
                result.links += 1
            except OSError as exc:
                result.errors.append(tr("не смог оставить ссылку на месте {path}: {error}", path=display(move.src),
                                         error=exc.strerror or exc))
    if duplicates:
        batches, errors = review.stage(duplicates, session, title=tr("сортировка"))
        result.duplicates_staged = sum(len(b.entries) for b in batches)
        result.errors += errors
    if moves and rules.get("links.update_office_recent", True):
        progress(tr("Поправляю «Недавние документы» Office…"))
        result.office = refs.update_office_mru(moves, [plan.folder], session)
    if moves and rules.get("links.update_shortcuts", True):
        progress(tr("Поправляю ярлыки и список недавних файлов…"))
        result.shortcuts = refs.update_shortcuts(moves, [plan.folder], session)
    return result
