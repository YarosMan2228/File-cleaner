"""Чтобы программы не потеряли перенесённые файлы.

  • находит, какие программы помнят пути (реестр HKCU и их настройки в AppData);
  • если файл помнит программа — на старом месте остаётся ссылка (симлинк или junction для папок);
  • «Недавние документы» Office и ярлыки Windows переписываются на новый путь.
Всё это пишется в журнал и откатывается командой undo.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

from . import config
from .fsutil import is_under, long_path, path_is_link
from .i18n import tr
from .winutil import NO_WINDOW, create_junction

try:
    import winreg
except ImportError:  # не Windows
    winreg = None

Progress = Callable[[str], None]
_TERMINATORS = set('"\'<>|\r\n\t\x00*?')
_SKIP_APPDATA = {
    "cache", "code cache", "gpucache", "node_modules", "temp", "crashpad", "shadercache", "dawncache",
    "service worker", "indexeddb", "blob_storage", "cacheddata", "logs", "packages", "extensions",
    "d3dscache", "npm-cache", "pip", "webcache", "grshadercache",
}
_CONFIG_EXTS = {".json", ".xml", ".ini", ".cfg", ".conf", ".config", ".yaml", ".yml", ".toml", ".vbox",
                ".properties", ".txt", ".settings", ".plist", ".vmls", ".vmx"}
_CONFIG_NAMES = {"preferences", "local state", "settings"}
# Эти ссылки программа исправляет сама, поэтому оставлять симлинк ради них не нужно.
HANDLED = {"office"}


def _quiet(_: str) -> None:
    pass


# ======================================================================= поиск путей в тексте
def _variants(prefix: str) -> list[tuple[str, str]]:
    low = prefix.lower().rstrip("\\/")
    return [(low, "\\"), (low.replace("\\", "/"), "/"), (low.replace("\\", "\\\\"), "\\\\")]


def extract_paths(text: str, prefixes: list[str]) -> list[tuple[int, int, str]]:
    """Пути внутри text, начинающиеся с prefixes: (начало, конец, путь с обратными слешами)."""
    low = text.lower()
    found: dict[int, tuple[int, int, str]] = {}
    for prefix in prefixes:
        for variant, sep in _variants(prefix):
            start = 0
            while (i := low.find(variant, start)) != -1:
                j = i + len(variant)
                start = j
                if j < len(text) and not text.startswith(sep, j) and text[j] not in _TERMINATORS:
                    continue  # «Downloads2» — это другая папка
                while j < len(text) and text[j] not in _TERMINATORS:
                    j += 1
                raw = text[i:j].rstrip(" ,;)]}")
                if sep == "\\\\":
                    path = raw.replace("\\\\", "\\")
                elif sep == "/":
                    path = raw.replace("/", "\\")
                else:
                    path = raw
                if i not in found or len(raw) > found[i][1] - found[i][0]:
                    found[i] = (i, i + len(raw), path)
    return sorted(found.values())


def _label_for_registry(key_path: str) -> str:
    parts = key_path.split("\\")
    if len(parts) > 2 and parts[1].lower() == "microsoft":
        return parts[2]
    return parts[1] if len(parts) > 1 else key_path


def scan_references(prefixes: list[Path], progress: Progress = _quiet, seconds: float = 45) -> dict[str, set[str]]:
    """Какие программы помнят пути под prefixes: {путь в нижнем регистре: {программа, …}}."""
    refs: dict[str, set[str]] = {}
    texts = [str(p) for p in prefixes]
    deadline = time.time() + seconds

    def note(text: str, label: str) -> None:
        for _, _, path in extract_paths(text, texts):
            refs.setdefault(os.path.normcase(path), set()).add(label)

    if winreg is not None:
        progress(tr("Смотрю, какие программы помнят эти файлы (реестр)…"))

        def walk_registry(key_path: str) -> None:
            if time.time() > deadline:
                return
            try:
                key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path)
            except OSError:
                return
            with key:
                subkeys, i = [], 0
                while True:
                    try:
                        _, value, kind = winreg.EnumValue(key, i)
                    except OSError:
                        break
                    i += 1
                    if kind in (winreg.REG_SZ, winreg.REG_EXPAND_SZ) and isinstance(value, str):
                        note(value, _label_for_registry(key_path))
                    elif kind == winreg.REG_MULTI_SZ and value:
                        note("\n".join(value), _label_for_registry(key_path))
                i = 0
                while True:
                    try:
                        subkeys.append(winreg.EnumKey(key, i))
                    except OSError:
                        break
                    i += 1
            for sub in subkeys:
                walk_registry(f"{key_path}\\{sub}")

        walk_registry("Software")

    progress(tr("Смотрю, какие программы помнят эти файлы (настройки программ)…"))
    for base in (config.APPDATA, config.LOCALAPPDATA):
        for dirpath, dirnames, filenames in os.walk(base):
            if time.time() > deadline:
                break
            dirnames[:] = [d for d in dirnames if d.lower() not in _SKIP_APPDATA]
            rel = os.path.relpath(dirpath, base).split(os.sep)
            label = rel[0] if rel and rel[0] != "." else base.name
            for name in filenames:
                low = name.lower()
                if os.path.splitext(low)[1] not in _CONFIG_EXTS and low not in _CONFIG_NAMES:
                    continue
                path = os.path.join(dirpath, name)
                try:
                    if os.path.getsize(path) > 2_000_000:
                        continue
                    with open(path, "rb") as fh:
                        data = fh.read()
                except OSError:
                    continue
                text = data.decode("utf-16-le", "ignore") if data[1:2] == b"\x00" else data.decode("utf-8", "ignore")
                note(text, label)
    return refs


def referenced_by(path: Path, refs: dict[str, set[str]], is_dir: bool) -> set[str]:
    """Программы, которые помнят этот файл (или что-то внутри этой папки)."""
    key = os.path.normcase(str(path))
    labels = set(refs.get(key, ()))
    if is_dir:
        prefix = key + os.sep
        for ref_key, names in refs.items():
            if ref_key.startswith(prefix):
                labels |= names
    return labels


def needs_link(labels: set[str]) -> bool:
    return any(label.lower() not in HANDLED for label in labels)


# ======================================================================= новые пути
class MoveMap:
    """Что куда переехало: для файлов — точные пути, для папок — всё, что внутри."""

    def __init__(self) -> None:
        self.files: dict[str, str] = {}
        self.dirs: dict[str, str] = {}

    def add(self, old: Path, new: Path, is_dir: bool) -> None:
        (self.dirs if is_dir else self.files)[os.path.normcase(str(old))] = str(new)

    def __bool__(self) -> bool:
        return bool(self.files or self.dirs)

    def new_path(self, path: str) -> str | None:
        key = os.path.normcase(path)
        if key in self.files:
            return self.files[key]
        current = key
        while True:
            parent = os.path.dirname(current)
            if parent == current:
                return None
            if parent in self.dirs:
                return self.dirs[parent] + path[len(parent):]
            current = parent

    def rewrite(self, text: str, prefixes: list[str]) -> str:
        """Заменяет в тексте старые пути на новые (с сохранением стиля слешей)."""
        pieces, last = [], 0
        for start, end, path in extract_paths(text, prefixes):
            new = self.new_path(path)
            if new is None:
                continue
            original = text[start:end]
            if "\\\\" in original:
                new = new.replace("\\", "\\\\")
            elif "/" in original and "\\" not in original:
                new = new.replace("\\", "/")
            pieces += [text[last:start], new]
            last = end
        if not pieces:
            return text
        pieces.append(text[last:])
        return "".join(pieces)


# ======================================================================= ссылки на старом месте
def leave_link(old: Path, new: Path, is_dir: bool, session) -> None:
    """Старый путь начинает «вести» в новый: junction для папки, символическая ссылка для файла."""
    if is_dir:
        create_junction(old, new)
    else:
        os.symlink(new, old)
    session.record("link", link=str(old), target=str(new), dir=is_dir)


def remove_link(link: Path, is_dir: bool) -> None:
    if not path_is_link(link):
        return
    if is_dir:
        os.rmdir(link)
    else:
        os.remove(link)


# ======================================================================= Office «Недавние»
_OFFICE_ROOT = r"Software\Microsoft\Office"


def update_office_mru(moves: MoveMap, prefixes: list[Path], session) -> int:
    """Переписывает пути в списках недавних документов Word/Excel/PowerPoint."""
    if winreg is None or not moves:
        return 0
    texts = [str(p) for p in prefixes]
    changed = 0

    def visit(key_path: str) -> None:
        nonlocal changed
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_READ | winreg.KEY_SET_VALUE)
        except OSError:
            try:
                key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path)
            except OSError:
                return
        with key:
            updates, i = [], 0
            while True:
                try:
                    name, value, kind = winreg.EnumValue(key, i)
                except OSError:
                    break
                i += 1
                if kind in (winreg.REG_SZ, winreg.REG_EXPAND_SZ) and isinstance(value, str):
                    new = moves.rewrite(value, texts)
                    if new != value:
                        updates.append((name, kind, value, new))
            for name, kind, old, new in updates:
                try:
                    winreg.SetValueEx(key, name, 0, kind, new)
                except OSError:
                    continue
                session.record("reg_set", key=key_path, name=name, kind=kind, old=old, new=new)
                changed += 1
            subkeys, i = [], 0
            while True:
                try:
                    subkeys.append(winreg.EnumKey(key, i))
                except OSError:
                    break
                i += 1
        for sub in subkeys:
            visit(f"{key_path}\\{sub}")

    visit(_OFFICE_ROOT)
    return changed


def restore_registry_value(entry: dict) -> None:
    if winreg is None:
        return
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, entry["key"], 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, entry["name"], 0, entry.get("kind", winreg.REG_SZ), entry["old"])


# ======================================================================= ярлыки
def shortcut_folders() -> list[tuple[Path, bool]]:
    """(папка, искать во вложенных) — где лежат ярлыки, которые стоит поправить."""
    return [
        (config.HOME / "Desktop", False),
        (config.APPDATA / "Microsoft" / "Windows" / "Start Menu", True),
        (config.APPDATA / "Microsoft" / "Windows" / "Recent", False),
        (config.APPDATA / "Microsoft" / "Office" / "Recent", False),
    ]


def find_shortcuts(prefixes: list[Path]) -> list[Path]:
    """.lnk, внутри которых встречается один из prefixes."""
    needles: set[bytes] = set()
    for prefix in prefixes:
        for text in (str(prefix), str(prefix).lower()):
            needles.add(text.encode("utf-16-le"))
            needles.add(text.encode("mbcs", "ignore") if os.name == "nt" else text.encode())
    needles.discard(b"")
    found = []
    for folder, recursive in shortcut_folders():
        if not folder.is_dir():
            continue
        items = folder.rglob("*.lnk") if recursive else folder.glob("*.lnk")
        for lnk in items:
            try:
                data = lnk.read_bytes()
            except OSError:
                continue
            lowered = data.lower()
            if any(n in data or n in lowered for n in needles):
                found.append(lnk)
    return found


_PS_READ = r"""
param($inFile, $outFile)
$items = Get-Content -Raw -Encoding UTF8 $inFile | ConvertFrom-Json
$sh = New-Object -ComObject WScript.Shell
$res = @(foreach ($p in $items) { try { $s = $sh.CreateShortcut($p)
  [pscustomobject]@{ lnk = $p; target = $s.TargetPath; workdir = $s.WorkingDirectory } } catch {} })
ConvertTo-Json -InputObject $res -Depth 3 | Out-File -Encoding utf8 $outFile
"""
_PS_WRITE = r"""
param($inFile, $outFile)
$items = Get-Content -Raw -Encoding UTF8 $inFile | ConvertFrom-Json
$sh = New-Object -ComObject WScript.Shell
$ok = @(foreach ($i in $items) { try { $s = $sh.CreateShortcut($i.lnk); $s.TargetPath = $i.target
  if ($i.workdir) { $s.WorkingDirectory = $i.workdir }; $s.Save(); $i.lnk } catch {} })
ConvertTo-Json -InputObject $ok | Out-File -Encoding utf8 $outFile
"""


def _powershell(script: str, payload: list) -> list:
    if not payload:
        return []
    with tempfile.TemporaryDirectory() as tmp:
        ps1, inp, out = Path(tmp, "run.ps1"), Path(tmp, "in.json"), Path(tmp, "out.json")
        ps1.write_text(script, encoding="utf-8-sig")
        inp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        try:
            subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ps1),
                            str(inp), str(out)], capture_output=True, timeout=600, creationflags=NO_WINDOW)
        except (OSError, subprocess.SubprocessError):
            return []
        if not out.exists():
            return []
        text = out.read_text(encoding="utf-8-sig").strip()
        data = json.loads(text) if text else []
        return data if isinstance(data, list) else [data]


def update_shortcuts(moves: MoveMap, prefixes: list[Path], session) -> int:
    """Переписывает ярлыки (Рабочий стол, Пуск, недавние файлы), которые вели на перенесённое."""
    if not moves:
        return 0
    candidates = find_shortcuts(prefixes)
    current = _powershell(_PS_READ, [str(p) for p in candidates])
    plan = []
    for item in current:
        target = item.get("target") or ""
        new_target = moves.new_path(target) if target else None
        if not new_target:
            continue
        workdir = item.get("workdir") or ""
        new_workdir = moves.new_path(workdir) if workdir else None
        plan.append({"lnk": item["lnk"], "target": new_target, "workdir": new_workdir or workdir,
                     "old_target": target, "old_workdir": workdir})
    done = set(_powershell(_PS_WRITE, plan))
    for item in plan:
        if item["lnk"] in done:
            session.record("lnk_set", lnk=item["lnk"], old=item["old_target"], new=item["target"],
                           old_workdir=item["old_workdir"], workdir=item["workdir"])
    return len(done)


def restore_shortcuts(entries: list[dict]) -> int:
    plan = [{"lnk": e["lnk"], "target": e["old"], "workdir": e.get("old_workdir", "")} for e in entries]
    return len(_powershell(_PS_WRITE, plan))


def is_inside(path: Path, roots: list[Path]) -> bool:
    return any(is_under(path, r) for r in roots)
