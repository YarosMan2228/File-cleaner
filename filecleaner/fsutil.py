"""Файловая система: безопасный обход, атрибуты Windows, перемещение без перезаписи, длинные пути."""
from __future__ import annotations

import errno
import os
import re
import shutil
import stat
import time
from collections.abc import Iterator
from pathlib import Path

from . import config
from .i18n import plural_words, tr

FILE_ATTRIBUTE_READONLY = 0x1
FILE_ATTRIBUTE_HIDDEN = 0x2
FILE_ATTRIBUTE_SYSTEM = 0x4
FILE_ATTRIBUTE_COMPRESSED = 0x800
FILE_ATTRIBUTE_OFFLINE = 0x1000
FILE_ATTRIBUTE_RECALL_ON_OPEN = 0x40000
FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x400000
_CLOUD_ONLY = FILE_ATTRIBUTE_OFFLINE | FILE_ATTRIBUTE_RECALL_ON_OPEN | FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS
_ERROR_NOT_SAME_DEVICE = 17


class PartialMoveError(OSError):
    """Папка скопирована на другой диск, но оригинал удалился не полностью."""


# ------------------------------------------------------------------ атрибуты
def attributes(st: os.stat_result) -> int:
    return getattr(st, "st_file_attributes", 0)


def is_cloud_only(st: os.stat_result) -> bool:
    """Файл OneDrive, которого нет на диске: его чтение запустит скачивание."""
    return bool(attributes(st) & _CLOUD_ONLY)


def is_hidden(st: os.stat_result) -> bool:
    return bool(attributes(st) & FILE_ATTRIBUTE_HIDDEN)


def is_hidden_system(st: os.stat_result) -> bool:
    attrs = attributes(st)
    return bool(attrs & FILE_ATTRIBUTE_HIDDEN and attrs & FILE_ATTRIBUTE_SYSTEM)


def birth_time(st: os.stat_result) -> float:
    return getattr(st, "st_birthtime", st.st_mtime)


def is_link(entry: os.DirEntry) -> bool:
    """Симлинк или junction: по ним не ходим, чтобы не уйти в чужие папки и не зациклиться."""
    if entry.is_symlink():
        return True
    is_junction = getattr(entry, "is_junction", None)
    return bool(is_junction and is_junction())


def path_is_link(path: Path | str) -> bool:
    path = os.fspath(path)
    return os.path.islink(path) or (hasattr(os.path, "isjunction") and os.path.isjunction(path))


# ------------------------------------------------------------------ обход
def list_dir(path: str) -> list[os.DirEntry] | None:
    try:
        with os.scandir(path) as it:
            return list(it)
    except OSError:
        return None


def is_protected_listing(names: set[str]) -> bool:
    """Папка проекта (.git, package.json…) или программы (есть .dll)."""
    if names & config.PROJECT_MARKERS:
        return True
    return any(name.endswith(config.PROTECTED_SUFFIXES) for name in names)


def looks_like_program_folder(names: set[str]) -> bool:
    """Установленная программа или репозиторий: раскладывать такую папку нельзя — программа сломается.

    Строже, чем is_protected_listing: одна скачанная .dll или requirements.txt в Загрузках — ещё не программа.
    """
    if names & {".git", ".hg", ".svn", "pyvenv.cfg"}:
        return True
    return sum(name.endswith((".dll", ".sys")) for name in names) >= 5


def skip_dir(entry: os.DirEntry) -> bool:
    name = entry.name.lower()
    if name.startswith(".") or name in config.SKIP_DIR_NAMES:
        return True
    try:
        st = entry.stat(follow_symlinks=False)
    except OSError:
        return True
    return is_hidden_system(st)


def is_drive_root(path: Path | str) -> bool:
    path = Path(path)
    return path.parent == path


def walk(root: Path, *, rules: bool = True) -> Iterator[tuple[os.DirEntry, bool]]:
    """Файлы под root и признак «лежит внутри программы или проекта».

    rules=False — без исключений (для папок кэша); по ссылкам не ходим в любом случае.
    Программа, установленная прямо в корень диска (B:\\Resolve.exe и .dll рядом), защищает только
    файлы в корне — папки на диске проверяются каждая сама по себе.
    """
    stack: list[tuple[str, bool, bool]] = [(str(root), True, False)]
    while stack:
        path, is_root, protected = stack.pop()
        entries = list_dir(path)
        if entries is None:
            continue
        files_protected = protected
        if rules and not protected:
            if not is_root:
                protected = files_protected = is_protected_listing({e.name.lower() for e in entries})
            elif is_drive_root(path):
                files_protected = is_protected_listing({e.name.lower() for e in entries})
        for entry in entries:
            try:
                if is_link(entry):
                    continue
                if entry.is_dir(follow_symlinks=False):
                    if not rules or not skip_dir(entry):
                        stack.append((entry.path, False, protected))
                elif entry.is_file(follow_symlinks=False):
                    yield entry, files_protected
            except OSError:
                continue


def find_empty_dirs(root: Path, min_age_days: float = 0, now: float | None = None) -> list[Path]:
    """Пустые папки под root: вложенные идут раньше родителей, сам root не включается."""
    now = now or time.time()
    found: list[Path] = []

    def visit(path: str, is_root: bool) -> bool:
        entries = list_dir(path)
        if entries is None:
            return False
        if not is_root and is_protected_listing({e.name.lower() for e in entries}):
            return False
        empty = True
        for entry in entries:
            try:
                subdir = entry.is_dir(follow_symlinks=False) and not is_link(entry)
                if subdir and not skip_dir(entry):
                    if not visit(entry.path, False):
                        empty = False
                else:
                    empty = False
            except OSError:
                empty = False
        if not empty or is_root:
            return empty
        try:
            old_enough = age_days(os.stat(path).st_mtime, now) >= min_age_days
        except OSError:
            return False
        if old_enough:
            found.append(Path(path))
        return old_enough

    visit(str(root), True)
    return found


def old_dirs(root: Path, min_age_days: float, now: float) -> list[Path]:
    """Подпапки root, которые давно не менялись (чтобы убрать их из Temp, когда опустеют)."""
    result: list[Path] = []
    stack = [str(root)]
    while stack:
        entries = list_dir(stack.pop())
        if entries is None:
            continue
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False) and not is_link(entry):
                    stack.append(entry.path)
                    if age_days(entry.stat(follow_symlinks=False).st_mtime, now) >= min_age_days:
                        result.append(Path(entry.path))
            except OSError:
                continue
    return result


# ------------------------------------------------------------------ пути
def long_path(path: Path | str) -> str:
    """Путь, который Windows откроет даже длиннее 260 символов."""
    text = os.path.abspath(os.fspath(path))
    if os.name != "nt" or len(text) < 240 or text.startswith("\\\\?\\"):
        return text
    if text.startswith("\\\\"):
        return "\\\\?\\UNC\\" + text[2:]
    return "\\\\?\\" + text


def key_of(path: Path | str) -> str:
    """Ключ для сравнения путей: в Windows регистр букв не важен."""
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def is_under(path: Path | str, root: Path | str) -> bool:
    p, r = key_of(path), key_of(root).rstrip("\\/")
    return p == r or p.startswith(r + os.sep)


def unique_path(path: Path, taken: set[str] | None = None) -> Path:
    """Свободное имя: «файл.pdf» → «файл (2).pdf», если занято."""
    def free(candidate: Path) -> bool:
        if taken is not None and key_of(candidate) in taken:
            return False
        return not os.path.lexists(long_path(candidate))

    if free(path):
        return path
    for i in range(2, 100_000):
        candidate = path.with_name(f"{path.stem} ({i}){path.suffix}")
        if free(candidate):
            return candidate
    raise FileExistsError(path)


def display(path: Path | str) -> str:
    """Короткий путь для экрана: домашняя папка заменяется на ~."""
    text = os.fspath(path)
    home = str(config.HOME)
    if text.lower().startswith(home.lower()):
        return "~" + text[len(home):]
    return text


# ------------------------------------------------------------------ изменения на диске
def _force_remove(func, path, _exc) -> None:
    """Снимает «только чтение» и пробует удалить ещё раз."""
    os.chmod(path, stat.S_IWRITE)
    func(path)


def remove_file(path: Path | str) -> None:
    target = long_path(path)
    try:
        os.remove(target)
    except PermissionError:
        os.chmod(target, stat.S_IWRITE)
        os.remove(target)


def remove_tree(path: Path | str) -> None:
    shutil.rmtree(long_path(path), onexc=_force_remove)


def move_path(src: Path | str, dst: Path | str) -> None:
    """Перемещает файл или папку. Никогда ничего не перезаписывает."""
    s, d = long_path(src), long_path(dst)
    if os.path.lexists(d):
        raise FileExistsError(errno.EEXIST, tr("Уже существует"), os.fspath(dst))
    try:
        os.rename(s, d)
        return
    except OSError as exc:
        if getattr(exc, "winerror", None) != _ERROR_NOT_SAME_DEVICE and exc.errno != errno.EXDEV:
            raise
    # Другой диск: сначала копия, потом удаление оригинала.
    if os.path.isdir(s) and not path_is_link(s):
        shutil.copytree(s, d, symlinks=True)
        try:
            remove_tree(s)
        except OSError as exc:
            raise PartialMoveError(errno.EIO, tr("Скопировано, но оригинал удалился не полностью: {error}", error=exc),
                                   os.fspath(src)) from exc
    else:
        shutil.copy2(s, d)
        try:
            remove_file(s)
        except OSError:
            remove_file(d)
            raise


# ------------------------------------------------------------------ числа и слова
def age_days(timestamp: float, now: float | None = None) -> float:
    return ((now or time.time()) - timestamp) / 86400


def human_size(n: float) -> str:
    size = float(n)
    units = [tr(unit) for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ")]
    for unit in units[:-1]:
        if abs(size) < 1024:
            return f"{size:.0f} {unit}" if unit == units[0] else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} {units[-1]}"


_SIZE_RE = re.compile(r"^\s*([\d.,]+)\s*([a-zа-я]*)\s*$", re.I)
_UNITS = {
    "": 1, "b": 1, "б": 1,
    "k": 1024, "kb": 1024, "кб": 1024,
    "m": 1024**2, "mb": 1024**2, "мб": 1024**2,
    "g": 1024**3, "gb": 1024**3, "гб": 1024**3,
    "t": 1024**4, "tb": 1024**4, "тб": 1024**4,
}


def parse_size(text: str | int) -> int:
    """«100KB», «1.5 ГБ», «500» → байты."""
    if isinstance(text, int):
        return text
    match = _SIZE_RE.match(text)
    if not match or match.group(2).lower() not in _UNITS:
        raise ValueError(tr("Непонятный размер: {text} (пример: 100KB, 1.5GB)", text=repr(text)))
    return int(float(match.group(1).replace(",", ".")) * _UNITS[match.group(2).lower()])


def plural(n: int, one: str, few: str, many: str) -> str:
    words = plural_words(one, few, many)
    if len(words) == 2:  # английский: одна форма для 1, другая для остальных
        return f"{n:,} {words[0] if n == 1 else words[1]}".replace(",", " ")
    n10, n100 = n % 10, n % 100
    if n10 == 1 and n100 != 11:
        word = one
    elif 2 <= n10 <= 4 and not 12 <= n100 <= 14:
        word = few
    else:
        word = many
    return f"{n:,} {word}".replace(",", " ")


def name_tokens(name: str) -> list[str]:
    """«VSCodeUserSetup-x64_1.85» → ['vs', 'code', 'user', 'setup', 'x', '64', '1', '85']."""
    spaced = re.sub(r"([a-zа-яё])([A-ZА-ЯЁ])", r"\1 \2", name)
    spaced = re.sub(r"([A-ZА-ЯЁ]+)([A-ZА-ЯЁ][a-zа-яё])", r"\1 \2", spaced)
    tokens: list[str] = []
    for part in re.split(r"[\W_]+", spaced.lower()):
        tokens.extend(re.findall(r"\d+|\D+", part))
    return [t for t in tokens if t]


def keyword_match(tokens: list[str], keyword: str) -> bool:
    """Слово целиком; длинные ключевые слова (от 4 букв) — ещё и как начало слова.

    Ключевое слово из нескольких частей («lab2» → lab, 2) ищется как те же части подряд в имени.
    """
    parts = name_tokens(keyword)
    if len(parts) > 1:
        return any(tokens[i:i + len(parts)] == parts for i in range(len(tokens) - len(parts) + 1))
    kw = keyword.lower()
    return any(t == kw or (len(kw) >= 4 and t.startswith(kw)) for t in tokens)
