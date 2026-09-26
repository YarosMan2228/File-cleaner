"""Сверка перед удалением: копия — с оригиналом байт в байт, архив — с папкой, куда его распаковали.

Архив можно удалять, только если каждый его файл лежит в папке с тем же размером и тем же содержимым.
zip программа читает сама — контрольные суммы файлов записаны в архиве. rar, 7z и остальное — через
7-Zip, если он установлен (его список тоже с контрольными суммами). Без 7-Zip архив распаковывается
во временную папку встроенным в Windows tar и сравнивается байт в байт. Сами архивы и папки не меняются.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import zipfile
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .fsutil import key_of, long_path, plural, walk
from .i18n import tr
from .winutil import NO_WINDOW, system_exe

Log = Callable[[str], None]
OK_CODES = ("ok", "same")                                            # можно удалять
BAD_CODES = ("missing", "differs", "not_same", "no_folder", "no_original")   # не удалять


def _quiet(_: str) -> None:
    pass


def _never() -> bool:
    return False


@dataclass
class Member:
    path: str             # путь внутри архива, через /
    size: int
    crc: int | None       # None — контрольной суммы нет: сверяем байт в байт


@dataclass
class Verdict:
    code: str             # см. text()
    files: int = 0        # сколько файлов сверено
    name: str = ""        # файл, который не сошёлся
    count: int = 0        # сколько файлов не хватает

    @property
    def ok(self) -> bool | None:
        """True — можно удалять, False — нельзя, None — проверить не получилось."""
        return True if self.code in OK_CODES else False if self.code in BAD_CODES else None

    def text(self) -> str:
        files = plural(self.files, "файл", "файла", "файлов")
        return {
            "ok": tr("проверено: все {files} из архива есть в папке и совпадают — можно удалять", files=files),
            "same": tr("проверено: совпадает с оригиналом байт в байт — можно удалять"),
            "missing": tr("в папке нет {count} из архива, например «{name}» — не удалять",
                          count=plural(self.count, "файл", "файла", "файлов"), name=self.name),
            "differs": tr("«{name}» в папке изменён, в архиве — другая версия: не удалять", name=self.name)
            if self.count <= 1 else tr("в папке изменены {count}, например «{name}» — в архиве другие версии: не удалять",
                                       count=plural(self.count, "файл", "файла", "файлов"), name=self.name),
            "not_same": tr("отличается от оригинала — не удалять"),
            "no_folder": tr("папки, куда распакован архив, нет на месте — не удалять"),
            "no_original": tr("оригинала нет на месте — не удалять"),
            "encrypted": tr("архив с паролем — содержимое не проверить"),
            "unreadable": tr("архив не открывается — возможно, повреждён"),
            "need_7zip": tr("Windows сама этот архив не открывает — установи 7-Zip (бесплатно) и проверь снова"),
            "empty": tr("в архиве нет файлов — проверять нечего"),
            "stopped": tr("проверка прервана"),
        }.get(self.code, self.code)

    def to_dict(self) -> dict:
        return {"code": self.code, "files": self.files, "name": self.name, "count": self.count}

    @classmethod
    def from_dict(cls, data: object) -> Verdict | None:
        if not isinstance(data, dict) or not data.get("code"):
            return None
        return cls(str(data["code"]), int(data.get("files") or 0), str(data.get("name") or ""), int(data.get("count") or 0))


# ------------------------------------------------------------------ копии
def same_bytes(a: str, b: str) -> bool:
    """Байт в байт — без кэша filecmp: тот помнит ответ по размеру и времени, а файл могли переписать."""
    with open(a, "rb") as fa, open(b, "rb") as fb:
        while True:
            chunk = fa.read(1024 * 1024)
            if chunk != fb.read(1024 * 1024):
                return False
            if not chunk:
                return True


def same_file(a: Path, b: Path) -> bool:
    try:
        return os.path.getsize(long_path(a)) == os.path.getsize(long_path(b)) and same_bytes(long_path(a), long_path(b))
    except OSError:
        return False


def same_tree(copy: Path, original: Path) -> bool:
    copy_files = {key_of(e.path)[len(key_of(copy)):]: e.path for e, _ in walk(copy, rules=False)}
    original_files = {key_of(e.path)[len(key_of(original)):] for e, _ in walk(original, rules=False)}
    if not copy_files or set(copy_files) != original_files:
        return False
    return all(same_file(Path(path), Path(key_of(original) + rel)) for rel, path in copy_files.items())


def check_copy(copy: Path, original: Path) -> Verdict:
    if not os.path.lexists(long_path(original)):
        return Verdict("no_original")
    same = same_tree(copy, original) if os.path.isdir(long_path(copy)) else same_file(copy, original)
    return Verdict("same" if same else "not_same", 1)


# ------------------------------------------------------------------ архивы
def seven_zip() -> Path | None:
    """7-Zip, если установлен: читает rar, 7z и почти все архивы, со списком контрольных сумм."""
    for env in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)"):
        base = os.environ.get(env)
        if base and (exe := Path(base) / "7-Zip" / "7z.exe").is_file():
            return exe
    return None


def _run(cmd: list[str], timeout: int) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(cmd, capture_output=True, stdin=subprocess.DEVNULL, timeout=timeout, creationflags=NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return None


def listing(archive: Path) -> list[Member] | str | None:
    """Файлы архива с контрольными суммами; "encrypted" — архив с паролем; None — без распаковки не прочитать."""
    if archive.suffix.lower() == ".zip":
        try:
            with zipfile.ZipFile(long_path(archive)) as z:
                infos = [i for i in z.infolist() if not i.is_dir()]
        except (OSError, zipfile.BadZipFile, RuntimeError, ValueError):
            return None
        if any(i.flag_bits & 0x1 for i in infos):
            return "encrypted"
        return [Member(i.filename, i.file_size, i.CRC) for i in infos]
    exe = seven_zip()
    out = _run([str(exe), "l", "-slt", "-ba", "-sccUTF-8", "-p-", str(archive)], 600) if exe else None
    if out is None or out.returncode != 0:
        return None
    members = []
    for block in out.stdout.decode("utf-8", "replace").replace("\r\n", "\n").split("\n\n"):
        fields = dict(line.split(" = ", 1) for line in block.splitlines() if " = " in line)
        if not fields.get("Path") or fields.get("Folder") == "+":
            continue
        if fields.get("Encrypted") == "+":
            return "encrypted"
        crc = fields.get("CRC", "").strip()
        members.append(Member(fields["Path"].replace("\\", "/"), int(fields.get("Size") or 0),
                              int(crc, 16) if crc else None))
    return members


def _extract(archive: Path, target: Path) -> str:
    """Распаковывает архив в target: "" — получилось, иначе код причины (unreadable, need_7zip)."""
    exe = seven_zip()
    if exe:
        out = _run([str(exe), "x", "-y", "-p-", "-bso0", "-bsp0", f"-o{target}", str(archive)], 3 * 3600)
        return "" if out is not None and out.returncode == 0 else "unreadable"
    # tar из Windows не понимает кириллицу в аргументах: архив — на вход, распаковка — в текущую папку.
    # Имена не на латинице внутри архива он тоже не пишет — тогда нужен 7-Zip.
    try:
        with open(long_path(archive), "rb") as fh:
            out = subprocess.run([system_exe("tar.exe"), "-xf", "-"], stdin=fh, cwd=target, capture_output=True,
                                 timeout=3 * 3600, creationflags=NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return "need_7zip"
    return "" if out.returncode == 0 else "need_7zip"


def _crc32(path: str) -> int:
    crc = 0
    with open(path, "rb") as fh:
        while chunk := fh.read(1024 * 1024):
            crc = zlib.crc32(chunk, crc)
    return crc


def _base(files: list[Member], folder: Path) -> tuple[Path, list[Member]]:
    """Где искать файлы архива: в самой папке или — если в архиве папка с тем же именем — в её родителе."""
    bases = [folder]
    prefix = folder.name.lower() + "/"
    if all(m.path.lower().startswith(prefix) for m in files):
        bases.append(folder.parent)
    found: tuple[Path, list[Member]] | None = None
    for base in bases:
        missing = [m for m in files if not os.path.isfile(long_path(base / m.path))]
        if found is None or len(missing) < len(found[1]):
            found = (base, missing)
    return found  # type: ignore[return-value]


def _compare(files: list[Member], folder: Path, label: str, same: Callable[[Member, str], bool],
             progress: Log, stop: Callable[[], bool]) -> Verdict:
    base, missing = _base(files, folder)
    if missing:
        return Verdict("missing", len(files), missing[0].path.rsplit("/", 1)[-1], len(missing))
    changed = []
    for i, member in enumerate(files, 1):
        if stop():
            return Verdict("stopped", len(files))
        progress(tr("Сверяю «{name}»: {i}/{total}", name=label, i=i, total=len(files)))
        path = long_path(base / member.path)
        try:
            matches = os.path.getsize(path) == member.size and same(member, path)
        except OSError:
            matches = False
        if not matches:
            changed.append(member.path.rsplit("/", 1)[-1])
    if changed:
        return Verdict("differs", len(files), changed[0], len(changed))
    return Verdict("ok", len(files))


def check_archive(archive: Path, folder: Path, progress: Log = _quiet, stop: Callable[[], bool] = _never) -> Verdict:
    """Всё ли, что есть в архиве, лежит в папке с тем же содержимым (тогда архив можно удалять)."""
    if not os.path.isdir(long_path(folder)):
        return Verdict("no_folder")
    members = listing(archive)
    if members == "encrypted":
        return Verdict("encrypted")
    if isinstance(members, list) and not members:
        return Verdict("empty")
    if isinstance(members, list) and all(m.crc is not None for m in members):
        return _compare(members, folder, archive.name, lambda m, path: _crc32(path) == m.crc, progress, stop)
    # Контрольных сумм нет — распаковываем во временную папку и сравниваем байт в байт.
    with tempfile.TemporaryDirectory(prefix="FileCleaner-verify-") as tmp:
        progress(tr("Распаковываю «{name}» во временную папку…", name=archive.name))
        if failed := _extract(archive, Path(tmp)):
            return Verdict(failed)
        extracted = []
        for dirpath, _, names in os.walk(tmp):
            for name in names:
                full = os.path.join(dirpath, name)
                extracted.append(Member(os.path.relpath(full, tmp).replace("\\", "/"), os.path.getsize(full), None))
        if not extracted:
            return Verdict("empty")
        return _compare(extracted, folder, archive.name,
                        lambda m, path: same_bytes(os.path.join(tmp, m.path), path), progress, stop)
