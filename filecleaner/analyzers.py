"""Проверка: каждый анализатор ищет своё и объясняет, почему файл, возможно, не нужен."""
from __future__ import annotations

import hashlib
import os
import re
import time
import zipfile
from collections import defaultdict
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

from . import config
from .fsutil import (
    age_days, display, find_empty_dirs, human_size, is_under, keyword_match, key_of, list_dir,
    long_path, name_tokens, old_dirs, plural, walk,
)
from .index import HEAD_BYTES, FileRec, Index
from .rules import Rules
from .winutil import installed_programs, recycle_bin_info, running_processes, virtualbox_disks

Progress = Callable[[str], None]
RECYCLE_BIN = Path("::Корзина")


@dataclass
class Finding:
    rule: str            # ключ правила, например duplicates.copy_names
    group: str           # раздел отчёта и имя подпапки в «Ready for approval»
    path: Path
    size: int
    mode: str            # delete / review / report
    reason: str          # почему файл, возможно, не нужен — человеческим языком
    is_dir: bool = False
    original: Path | None = None   # что остаётся (оригинал для дубликата, папка для архива)
    count: int = 1                 # для папок — сколько файлов внутри


@dataclass
class CheckResult:
    findings: list[Finding]
    notes: list[str] = field(default_factory=list)
    prune_dirs: list[Path] = field(default_factory=list)   # папки Temp, которые можно убрать, когда опустеют

    def by_mode(self, mode: str) -> list[Finding]:
        return [f for f in self.findings if f.mode == mode]


def _quiet(_: str) -> None:
    pass


# ======================================================================= системный мусор
def _cache_files(root: Path, min_age_days: float, now: float) -> Iterator[tuple[Path, int]]:
    for entry, _ in walk(root, rules=False):
        try:
            st = entry.stat(follow_symlinks=False)
        except OSError:
            continue
        if min_age_days and age_days(st.st_mtime, now) < min_age_days:
            continue
        yield Path(entry.path), st.st_size


def chromium_cache_dirs(user_data: Path) -> list[Path]:
    """Папки кэша браузера на движке Chromium во всех профилях."""
    if not user_data.is_dir():
        return []
    dirs = [user_data / name for name in config.CHROMIUM_SHARED_CACHES]
    candidates = [user_data]
    try:
        candidates += [p for p in user_data.iterdir() if p.is_dir()]
    except OSError:
        pass
    for profile in candidates:
        if (profile / "Preferences").is_file():
            dirs += [profile / name for name in config.CHROMIUM_PROFILE_CACHES]
    return [d for d in dirs if d.is_dir()]


def system_junk(rules: Rules, now: float, progress: Progress = _quiet) -> tuple[list[Finding], list[str], list[Path]]:
    findings: list[Finding] = []
    notes: list[str] = []
    prune: list[Path] = []
    seen: set[str] = set()
    running = running_processes()

    def add(rule: str, group: str, dirs: list[Path], reason: str, min_age: float = 0) -> None:
        mode = rules.mode(rule)
        if mode == "off":
            return
        for folder in dirs:
            if not folder.is_dir():
                continue
            if list_dir(str(folder)) is None:
                notes.append(f"Нет доступа к {folder} — запусти программу от имени администратора, чтобы почистить.")
                continue
            progress(f"Проверяю {group.lower()}: {display(folder)}")
            for path, size in _cache_files(folder, min_age, now):
                key = os.path.normcase(str(path))
                if key not in seen:
                    seen.add(key)
                    findings.append(Finding(rule, group, path, size, mode, reason))

    temp_age = float(rules.get("junk.temp_min_age_days", 2))
    add("junk.temp", "Временные файлы Windows", config.TEMP_DIRS,
        f"временный файл, не менялся больше {temp_age:g} дн.", temp_age)
    if rules.mode("junk.temp") == "delete":
        for folder in config.TEMP_DIRS:
            if folder.is_dir():
                prune += old_dirs(folder, temp_age, now)
    add("junk.crash_dumps", "Отчёты о сбоях", config.CRASH_DIRS, "дамп или отчёт о сбое программы")

    browser_mode = rules.mode("junk.browser_cache")
    for name, process, user_data, extra in config.BROWSERS:
        dirs = chromium_cache_dirs(user_data) + [d for d in extra if d.is_dir()]
        if not dirs or browser_mode == "off":
            continue
        if process in running:
            notes.append(f"{name} запущен — закрой его, чтобы почистить кэш.")
            continue
        add("junk.browser_cache", "Кэш браузеров", dirs, f"кэш {name}: страницы и картинки, скачаются заново")
    if config.FIREFOX_PROFILES.is_dir() and browser_mode != "off":
        if "firefox.exe" in running:
            notes.append("Firefox запущен — закрой его, чтобы почистить кэш.")
        else:
            profiles = [p / "cache2" for p in config.FIREFOX_PROFILES.iterdir() if (p / "cache2").is_dir()]
            add("junk.browser_cache", "Кэш браузеров", profiles, "кэш Firefox: страницы и картинки, скачаются заново")

    for name, process, dirs in config.APP_CACHES:
        existing = [d for d in dirs if d.is_dir()]
        if not existing or rules.mode("junk.app_cache") == "off":
            continue
        if process in running:
            notes.append(f"{name} запущен — закрой его, чтобы почистить кэш.")
            continue
        add("junk.app_cache", "Кэш приложений", existing, f"кэш {name}, создастся заново")

    for name, folder in config.DEV_CACHES:
        add("junk.dev_cache", "Кэш разработки", [folder], f"кэш {name}: пакеты скачаются заново при установке")
    add("junk.shader_cache", "Кэш шейдеров видеокарты", config.SHADER_CACHES,
        "кэш шейдеров: игры соберут его заново (первый запуск может подтормаживать)")

    bin_mode = rules.mode("junk.recycle_bin")
    if bin_mode != "off":
        size, items = recycle_bin_info()
        if items:
            findings.append(Finding("junk.recycle_bin", "Корзина Windows", RECYCLE_BIN, size, bin_mode,
                                    f"{plural(items, 'объект', 'объекта', 'объектов')} в Корзине",
                                    count=items))
    return findings, notes, prune


# ======================================================================= хлам среди файлов
_MEDIA_TYPES = {"Видео", "Музыка", "Изображения", "Документы", "Таблицы", "Презентации"}


def user_junk(recs: list[FileRec], rules: Rules, now: float) -> list[Finding]:
    partial_age = float(rules.get("files.partial_min_age_days", 7))
    out = []
    for rec in recs:
        if rec.cloud or rec.protected:
            continue
        low = rec.name.lower()
        age = age_days(rec.mtime, now)
        force_report = False
        if low in ("thumbs.db", "ehthumbs.db", ".ds_store") or (low.startswith("._") and rec.size <= 4096):
            rule, group, reason = "files.thumbs", "Служебные файлы", "миниатюры/служебный файл, система создаст заново"
        elif low.startswith("~$") and rec.size < 8192 and age >= 1:
            rule, group, reason = "files.office_locks", "Временные файлы Office", "остался от закрытого документа Office"
        elif rec.ext == "tmp" and age >= 1:
            rule, group = "files.office_locks", "Временные файлы (.tmp)"
            inner = config.EXT_TO_TYPE.get(Path(rec.path.stem).suffix.lower().lstrip("."))
            if inner in _MEDIA_TYPES or rec.size >= 1024 * 1024:
                # «video123.mp4.tmp» от Zoom и т.п. — может быть единственной копией незаконченной записи.
                force_report = True
                reason = "похоже на незавершённую запись или конвертацию — возможно, это единственная копия"
            else:
                reason = f"временный .tmp, не менялся {age:.0f} дн."
        elif rec.ext in config.PARTIAL_EXTS and age >= partial_age:
            rule, group, reason = ("files.partial_downloads", "Недокачанные файлы",
                                   f"загрузка не завершилась, файл не менялся {age:.0f} дн.")
        else:
            continue
        mode = rules.mode(rule)
        if mode != "off":
            out.append(Finding(rule, group, rec.path, rec.size, "report" if force_report else mode, reason))
    return out


def empty_dirs(roots: dict[str, Path], rules: Rules, now: float) -> list[Finding]:
    """Пустые папки прямо в Загрузках и на Рабочем столе (целиком пустые, вместе с пустыми вложенными).

    Пустые папки внутри других папок не трогаем: это может быть часть структуры (бэкап, проект).
    """
    mode = rules.mode("files.empty_dirs")
    if mode == "off":
        return []
    min_age = float(rules.get("files.empty_dirs_min_age_days", 1))
    out = []
    for name in ("downloads", "desktop"):
        root = roots.get(name)
        if root is None:
            continue
        found = find_empty_dirs(root, min_age, now)
        empty = {key_of(d) for d in found}
        root_key = key_of(root)
        for folder in found:
            top = os.path.join(root_key, key_of(folder)[len(root_key):].lstrip("\\/").split(os.sep)[0])
            if top in empty:
                out.append(Finding("files.empty_dirs", "Пустые папки", folder, 0, mode, "пустая папка",
                                   is_dir=True, count=0))
    return out


# ======================================================================= дубликаты
_COPY_SUFFIX = re.compile(r"(\s*\(\d+\)|[\s_\-–—]+(copy|копия|копія|kopija|kopie)(\s*\(?\d+\)?)?)$", re.I)
_COPY_PREFIX = re.compile(r"^(copy of|копия|копія)\s", re.I)


def looks_like_copy(stem: str) -> bool:
    """«отчёт (1)», «отчёт — копия», «Copy of отчёт»."""
    return bool(_COPY_SUFFIX.search(stem) or _COPY_PREFIX.search(stem))


def dump_folders(roots: dict[str, Path], rules: Rules) -> list[Path]:
    """Папки из [scan] dump_folders: имена (downloads, desktop) или пути ("B:/downloads")."""
    folders = None
    result: list[Path] = []
    for item in rules.get("scan.dump_folders", ["downloads", "desktop"]) or []:
        path = roots.get(item)
        if path is None and item in config.FOLDER_TITLES:
            folders = folders if folders is not None else config.user_folders()
            path = folders.get(item)
        elif path is None:
            path = Path(item).expanduser()
        if path is not None and path.is_dir():
            result.append(path)
    return result


def dump_dirs(roots: dict[str, Path], rules: Rules) -> set[str]:
    """Папки-«свалки», куда всё падает само: Загрузки, Рабочий стол, Telegram Desktop, Temp."""
    dirs = {key_of(config.TEMP)}
    for root in dump_folders(roots, rules):
        dirs.add(key_of(root))
        for keep in rules.get("sort.keep_folders", []) or []:
            dirs.add(key_of(root / keep))
    return dirs


def _loose(path: Path, dump: set[str]) -> bool:
    """Лежит прямо в «свалке» (а не внутри распакованного дистрибутива или проекта в ней)."""
    return key_of(path.parent) in dump


def _identical_groups(index: Index, recs: list[FileRec], progress: Progress) -> list[list[FileRec]]:
    """Группы одинаковых файлов: размер → хэш начала и конца → полный хэш. Жёсткие ссылки — не дубли."""
    by_size: dict[int, list[FileRec]] = defaultdict(list)
    for rec in recs:
        by_size[rec.size].append(rec)
    candidates = [r for group in by_size.values() if len(group) > 1 for r in group]
    heads = index.hashes(candidates, "head", progress)
    by_head: dict[tuple[int, str], list[FileRec]] = defaultdict(list)
    for rec in candidates:
        if rec.key in heads:
            by_head[(rec.size, heads[rec.key])].append(rec)
    candidates = [r for group in by_head.values() if len(group) > 1 for r in group]
    fulls = index.hashes([r for r in candidates if r.size > 2 * HEAD_BYTES], "full", progress)
    groups: dict[tuple[int, str], list[FileRec]] = defaultdict(list)
    for rec in candidates:
        digest = heads[rec.key] if rec.size <= 2 * HEAD_BYTES else fulls.get(rec.key)
        if digest:
            groups[(rec.size, digest)].append(rec)
    result = []
    for group in groups.values():
        if len(group) < 2:
            continue
        physical: dict[tuple[int, int], FileRec] = {}
        for rec in group:
            try:
                st = os.stat(long_path(rec.path))
            except OSError:
                continue
            physical.setdefault((st.st_dev, st.st_ino), rec)
        if len(physical) > 1:
            result.append(list(physical.values()))
    return result


def _duplicate_folders(index: Index, recs: list[FileRec], roots: dict[str, Path], rules: Rules,
                       dump: set[str], progress: Progress) -> tuple[list[Finding], list[Path]]:
    """Одинаковые папки целиком: те же файлы с теми же именами и содержимым."""
    mode = rules.mode("duplicates.folders")
    if mode == "off":
        return [], []
    min_total = rules.size("duplicates.min_folder_size", "10MB")
    root_keys = {name: key_of(path) for name, path in roots.items()}
    stats: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    has_cloud: set[str] = set()
    for rec in recs:
        root_key = root_keys.get(rec.root)
        if root_key is None:
            continue
        parent = os.path.dirname(rec.key)
        while len(parent) > len(root_key) and parent.startswith(root_key):
            if rec.cloud:
                has_cloud.add(parent)
            else:
                entry = stats[parent]
                entry[0] += 1
                entry[1] += rec.size
            parent = os.path.dirname(parent)
    buckets: dict[tuple[int, int], list[str]] = defaultdict(list)
    for folder, (count, total) in stats.items():
        # Папка из одного файла — это просто дубликат файла, его найдёт проверка файлов.
        if total >= min_total and count >= 2 and folder not in has_cloud:
            buckets[(count, total)].append(folder)
    candidate_dirs = {d for group in buckets.values() if len(group) > 1 for d in group}
    if not candidate_dirs:
        return [], []

    members: dict[str, list[FileRec]] = defaultdict(list)
    for rec in recs:
        if rec.cloud:
            continue
        parent = os.path.dirname(rec.key)
        while parent and parent != os.path.dirname(parent):
            if parent in candidate_dirs:
                members[parent].append(rec)
            parent = os.path.dirname(parent)
    all_files = {r.key: r for group in members.values() for r in group}
    small = [r for r in all_files.values() if r.size <= 2 * HEAD_BYTES]
    large = [r for r in all_files.values() if r.size > 2 * HEAD_BYTES]
    digests = index.hashes(small, "head", progress)
    digests.update(index.hashes(large, "full", progress))

    signatures: dict[str, list[str]] = defaultdict(list)
    for folder, files in members.items():
        parts = []
        for rec in files:
            digest = digests.get(rec.key)
            if digest is None:
                break
            parts.append(f"{rec.key[len(folder):]}\0{rec.size}\0{digest}")
        else:
            signature = hashlib.blake2b("\n".join(sorted(parts)).encode(), digest_size=20).hexdigest()
            signatures[signature].append(folder)

    paths = {os.path.normcase(str(r.path.parent)): r.path.parent for r in all_files.values()}
    groups = sorted((g for g in signatures.values() if len(g) > 1), key=lambda g: min(d.count(os.sep) for d in g))
    covered: list[str] = []
    findings: list[Finding] = []
    removed: list[Path] = []
    for group in groups:
        if any(d == c or d.startswith(c + os.sep) for d in group for c in covered):
            continue
        folders = [_real_folder(d, paths) for d in group]

        def keeper_key(folder: Path) -> tuple:
            try:
                created = os.stat(long_path(folder)).st_birthtime
            except (OSError, AttributeError):
                created = 0.0
            return (looks_like_copy(folder.name), _loose(folder, dump), created, len(folder.parts))

        keeper = min(folders, key=keeper_key)
        count, total = stats[key_of(keeper)]
        for folder in folders:
            if folder == keeper:
                continue
            # Та же логика, что для файлов: копия рядом, «Папка (2)» или в Загрузках — на проверку,
            # копия в другой «осмысленной» папке — только отчёт.
            obvious = (key_of(folder.parent) == key_of(keeper.parent) or looks_like_copy(folder.name)
                       or _loose(folder, dump))
            rule = "duplicates.folders" if obvious else "duplicates.other"
            folder_mode = rules.mode(rule)
            if folder_mode == "off":
                continue
            findings.append(Finding(
                rule, "Дубликаты - папки целиком" if obvious else "Дубликаты - в разных папках", folder, total,
                folder_mode, f"точно такая же папка: {display(keeper)} ({plural(count, 'файл', 'файла', 'файлов')})",
                is_dir=True, original=keeper, count=count,
            ))
            removed.append(folder)
        covered += group
    return findings, removed


def _real_folder(key: str, known: dict[str, Path]) -> Path:
    """Путь папки с настоящим регистром букв (ключи индекса — в нижнем регистре)."""
    if key in known:
        return known[key]
    for known_key, path in known.items():
        if known_key.startswith(key + os.sep):
            depth = known_key[len(key):].count(os.sep)
            return path.parents[depth - 1]
    return Path(key)


def duplicates(index: Index, recs: list[FileRec], roots: dict[str, Path], rules: Rules,
               dump: set[str], progress: Progress = _quiet) -> list[Finding]:
    usable = [r for r in recs if not r.cloud]
    findings, removed_dirs = _duplicate_folders(index, usable, roots, rules, dump, progress)
    removed_keys = [key_of(d) for d in removed_dirs]
    min_size = rules.size("duplicates.min_size", "100KB")
    candidates = [
        r for r in usable
        if r.size >= min_size and not r.protected
        and not any(r.key.startswith(k + os.sep) for k in removed_keys)
    ]
    for group in _identical_groups(index, candidates, progress):
        keeper = min(group, key=lambda r: (looks_like_copy(r.path.stem), _loose(r.path, dump),
                                           r.ctime, len(r.path.parts), len(str(r.path))))
        for rec in group:
            if rec is keeper:
                continue
            if os.path.normcase(str(rec.path.parent)) == os.path.normcase(str(keeper.path.parent)):
                rule, group_name = "duplicates.same_folder", "Дубликаты - рядом с оригиналом"
                reason = f"рядом лежит такой же файл: {keeper.name}"
            elif looks_like_copy(rec.path.stem):
                rule, group_name = "duplicates.copy_names", "Дубликаты - имя-копия"
                reason = f"имя похоже на копию, оригинал: {display(keeper.path)}"
            elif _loose(rec.path, dump):
                rule, group_name = "duplicates.in_downloads", "Дубликаты - лишние в Загрузках"
                reason = f"оригинал лежит в {display(keeper.path)}"
            else:
                rule, group_name = "duplicates.other", "Дубликаты - в разных папках"
                reason = f"такой же файл: {display(keeper.path)} — возможно, копия нужна тут специально"
            mode = rules.mode(rule)
            if mode != "off":
                findings.append(Finding(rule, group_name, rec.path, rec.size, mode, reason, original=keeper.path))
    return findings


# ======================================================================= архивы
def archive_folder(path: Path) -> Path | None:
    """Папка рядом с архивом с тем же именем (так её называет «Извлечь всё»)."""
    lower = path.name.lower()
    base = path.stem
    for double in (".tar.gz", ".tar.bz2", ".tar.xz", ".tar.zst"):
        if lower.endswith(double):
            base = path.name[: -len(double)]
    folder = path.with_name(base)
    entries = list_dir(str(folder))
    return folder if entries else None


def zip_extracted_ratio(path: Path, folder: Path) -> float | None:
    """Какая доля файлов из zip лежит в папке с тем же размером."""
    try:
        with zipfile.ZipFile(long_path(path)) as archive:
            entries = [(info.filename, info.file_size) for info in archive.infolist() if not info.is_dir()]
    except (OSError, zipfile.BadZipFile, RuntimeError, ValueError):
        return None
    if not entries:
        return None
    best = 0.0
    for base in (folder, folder.parent):
        hits = 0
        for name, size in entries:
            try:
                if os.stat(long_path(base / name)).st_size == size:
                    hits += 1
            except (OSError, ValueError):
                pass
        best = max(best, hits / len(entries))
    return best


def extracted_archives(recs: list[FileRec], rules: Rules) -> list[Finding]:
    mode = rules.mode("archives.extracted")
    if mode == "off":
        return []
    out = []
    for rec in recs:
        if rec.cloud or rec.protected or rec.ext not in config.ARCHIVE_EXTS:
            continue
        folder = archive_folder(rec.path)
        if folder is None:
            continue
        if rec.ext == "zip":
            ratio = zip_extracted_ratio(rec.path, folder)
            if ratio is None or ratio < 0.3:
                continue
            if ratio < 0.95:
                out.append(Finding("archives.extracted", "Распакованные архивы", rec.path, rec.size, "report",
                                   f"распакован частично ({ratio:.0%} файлов) в «{folder.name}»", original=folder))
                continue
            reason = f"уже распакован в «{folder.name}» — все файлы на месте"
        else:
            reason = f"рядом папка «{folder.name}» — похоже, архив распакован (содержимое не проверял)"
        out.append(Finding("archives.extracted", "Распакованные архивы", rec.path, rec.size, mode, reason,
                           original=folder))
    return out


# ======================================================================= установщики
_INSTALLER_NOISE = {
    "setup", "installer", "install", "x", "win", "windows", "amd", "arm", "bit", "full", "offline", "online",
    "web", "user", "system", "portable", "release", "stable", "latest", "final", "en", "us", "ru", "multi",
    "exe", "msi", "v", "build", "update", "ver", "version", "beta", "rc", "lts", "pc",
}


def product_tokens(stem: str) -> tuple[str, ...]:
    return tuple(t for t in name_tokens(stem) if not t.isdigit() and len(t) >= 2 and t not in _INSTALLER_NOISE)


def _base_stem(stem: str) -> str:
    """Имя без «(1)», «— копия»: «setup-2.1 (1)» → «setup-2.1»."""
    return _COPY_PREFIX.sub("", _COPY_SUFFIX.sub("", stem)).strip()


_ARCH = re.compile(r"x86[_-]64|x64|x86|amd64|arm64|aarch64|win64|win32|(32|64)[-_ ]?bit|(?<=[_\-. ])(32|64)(?=$|[_\-. ])",
                   re.I)


def _version(stem: str) -> tuple[int, ...]:
    """Номер версии из имени; разрядность (x64, _64, 64-bit) — не версия."""
    return tuple(int(n) for n in re.findall(r"\d+", _ARCH.sub(" ", _base_stem(stem)))[:6])


def installers(recs: list[FileRec], rules: Rules, dump: set[str]) -> list[Finding]:
    """Только установщики, которые лежат сами по себе в Загрузках/на Рабочем столе.

    Установщики внутри распакованных дистрибутивов не трогаем — без них дистрибутив сломается.
    """
    modes = {k: rules.mode(f"installers.{k}") for k in ("installed", "old_versions", "other")}
    if all(m == "off" for m in modes.values()):
        return []
    candidates = [r for r in recs if r.ext in config.INSTALLER_EXTS and _loose(r.path, dump)
                  and not r.cloud and not r.protected]
    programs = [(name, set(name_tokens(name))) for name in installed_programs()]
    out: list[Finding] = []
    by_product: dict[tuple[str, ...], list[FileRec]] = defaultdict(list)
    for rec in candidates:
        tokens = product_tokens(_base_stem(rec.path.stem))
        if tokens:
            by_product[tokens].append(rec)
    old: dict[str, FileRec] = {}
    for group in by_product.values():
        if len(group) < 2:
            continue
        newest = max(group, key=lambda r: (_version(r.path.stem), r.mtime))
        for rec in group:
            # Сравниваем только настоящие номера версий. «file (1).msi» той же версии — это копия
            # (её найдёт поиск дубликатов), а файл без версии в имени сравнить не с чем.
            mine, best = _version(rec.path.stem), _version(newest.path.stem)
            if mine and best and mine < best:
                old[rec.key] = newest
    for rec in candidates:
        tokens = product_tokens(_base_stem(rec.path.stem))
        if rec.key in old and modes["old_versions"] != "off":
            out.append(Finding("installers.old_versions", "Установщики - старые версии", rec.path, rec.size,
                               modes["old_versions"], f"есть более новый: {old[rec.key].name}",
                               original=old[rec.key].path))
            continue
        match = next((name for name, words in programs if tokens and set(tokens) <= words), None)
        if match and modes["installed"] != "off":
            out.append(Finding("installers.installed", "Установщики - программа уже стоит", rec.path, rec.size,
                               modes["installed"], f"программа уже установлена: {match}"))
        elif modes["other"] != "off":
            out.append(Finding("installers.other", "Установщики - остальные", rec.path, rec.size, modes["other"],
                               "установщик; среди установленных программ не нашёл — возможно, ещё нужен"))
    return out


# ======================================================================= диски виртуалок
def vm_disks(recs: list[FileRec], rules: Rules) -> list[Finding]:
    mode = rules.mode("vm_disks.unregistered")
    if mode == "off":
        return []
    registered = virtualbox_disks()
    if registered is None:
        return []
    names: dict[str, str] = {os.path.basename(key): path for key, path in registered.items()}
    out = []
    for rec in recs:
        if rec.ext != "vdi" or rec.cloud or os.path.normcase(str(rec.path)) in registered:
            continue
        reason = "диск VirtualBox не подключён ни к одной виртуалке"
        twin = names.get(rec.name.lower())
        if twin:
            reason += f"; подключён другой диск с таким же именем: {twin}"
        out.append(Finding("vm_disks.unregistered", "Неподключённые диски виртуалок", rec.path, rec.size, mode, reason))
    return out


# ======================================================================= старые большие файлы
def old_files(recs: list[FileRec], rules: Rules, now: float, taken: set[str]) -> list[Finding]:
    mode = rules.mode("old_files")
    if mode == "off":
        return []
    min_size = rules.size("old_files.min_size", "500MB")
    min_age = float(rules.get("old_files.min_age_days", 365))
    out = []
    for rec in recs:
        if rec.cloud or rec.key in taken or rec.size < min_size:
            continue
        age = age_days(rec.mtime, now)
        if age >= min_age:
            years = age / 365
            when = f"{years:.1f} г." if years >= 1 else f"{age:.0f} дн."
            out.append(Finding("old_files", "Старые большие файлы", rec.path, rec.size, mode,
                               f"не менялся {when} — возраст ≠ ненужность, реши сам (или перенеси в архив)"))
    return out


# ======================================================================= сборка
_PRIORITY = ("junk.", "duplicates.folders", "duplicates.", "archives.", "installers.", "vm_disks.", "files.",
             "old_files")


def _priority(finding: Finding) -> int:
    return next((i for i, prefix in enumerate(_PRIORITY) if finding.rule.startswith(prefix)), len(_PRIORITY))


def resolve(findings: list[Finding]) -> list[Finding]:
    """Один файл — одна причина: оставляем самую сильную. Файлы внутри найденных папок не дублируем."""
    chosen: dict[str, Finding] = {}
    folders: list[str] = []
    for finding in sorted(findings, key=_priority):
        key = key_of(finding.path)
        if key in chosen or any(key.startswith(f + os.sep) for f in folders):
            continue
        chosen[key] = finding
        if finding.is_dir and finding.mode in ("review", "delete"):
            folders.append(key)
    return list(chosen.values())


def protection(rules: Rules, sorting: bool = False) -> Callable[[Path], str | None]:
    """Проверка по правилам [protect]: вернёт правило, которое защищает путь, или None.

    paths и keep_keywords — «не трогать совсем». name_keywords (паспорт, договор…) защищают только
    от удаления: раскладывать такие документы по секторам можно (sorting=True их пропускает).
    """
    paths = [Path(p).expanduser() for p in rules.get("protect.paths", []) or []]
    keywords = [k for k in rules.get("protect.keep_keywords", []) or [] if k]
    if not sorting:
        keywords += [k for k in rules.get("protect.name_keywords", []) or [] if k]

    def protected_by(path: Path) -> str | None:
        tokens = name_tokens(path.name)
        return next((str(p) for p in paths if is_under(path, p)), None) or \
            next((k for k in keywords if keyword_match(tokens, k)), None)

    return protected_by


def apply_protection(findings: list[Finding], rules: Rules) -> None:
    """Правила [protect]: такие файлы только в отчёт."""
    protected_by = protection(rules)
    for finding in findings:
        if finding.rule.startswith("junk.") or finding.mode == "report":
            continue
        hit = protected_by(finding.path)
        if hit:
            finding.mode = "report"
            finding.reason += f" — защищено правилом «{hit}»"


def run_check(index: Index, roots: dict[str, Path], rules: Rules, progress: Progress = _quiet,
              now: float | None = None) -> CheckResult:
    now = now or time.time()
    fresh = float(rules.get("scan.min_file_age_minutes", 30)) / (24 * 60)
    recs = [r for r in index.files(roots.keys()) if age_days(r.mtime, now) >= fresh]
    dump = dump_dirs(roots, rules)

    findings, notes, prune = system_junk(rules, now, progress)
    progress("Ищу хлам среди файлов…")
    findings += user_junk(recs, rules, now)
    findings += empty_dirs(roots, rules, now)
    findings += duplicates(index, recs, roots, rules, dump, progress)
    progress("Проверяю архивы, установщики и диски виртуалок…")
    findings += extracted_archives(recs, rules)
    findings += installers(recs, rules, dump)
    findings += vm_disks(recs, rules)
    findings += old_files(recs, rules, now, {key_of(f.path) for f in findings})
    findings = resolve(findings)
    apply_protection(findings, rules)
    return CheckResult([f for f in findings if f.mode != "off"], notes, prune)


def group_totals(findings: list[Finding]) -> list[tuple[str, str, int, int]]:
    """(группа, режим, объектов, байт) — для сводки, самые большие сверху."""
    totals: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0])
    for f in findings:
        entry = totals[(f.group, f.mode)]
        entry[0] += f.count if f.rule == "junk.recycle_bin" else 1
        entry[1] += f.size
    rows = [(group, mode, n, size) for (group, mode), (n, size) in totals.items()]
    return sorted(rows, key=lambda r: r[3], reverse=True)


__all__ = ["Finding", "CheckResult", "run_check", "group_totals", "looks_like_copy", "human_size"]
