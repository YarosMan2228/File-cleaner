"""Обёртки над Windows API и системными командами — без сторонних библиотек."""
from __future__ import annotations

import ctypes
import os
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

from . import config
from .fsutil import long_path

try:
    import winreg
except ImportError:  # не Windows
    winreg = None

IS_WINDOWS = os.name == "nt"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

if IS_WINDOWS:
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.GetCompressedFileSizeW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
    _kernel32.GetCompressedFileSizeW.restype = wintypes.DWORD

    class _SHQUERYRBINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("i64Size", ctypes.c_longlong), ("i64NumItems", ctypes.c_longlong)]


# ------------------------------------------------------------------ корзина и диски
def recycle_bin_info() -> tuple[int, int]:
    """(байт, объектов) в Корзине на всех дисках."""
    if not IS_WINDOWS:
        return 0, 0
    info = _SHQUERYRBINFO()
    info.cbSize = ctypes.sizeof(info)
    if ctypes.windll.shell32.SHQueryRecycleBinW(None, ctypes.byref(info)) != 0:
        return 0, 0
    return int(info.i64Size), int(info.i64NumItems)


def empty_recycle_bin() -> bool:
    no_confirm, no_progress, no_sound = 0x1, 0x2, 0x4
    return ctypes.windll.shell32.SHEmptyRecycleBinW(None, None, no_confirm | no_progress | no_sound) == 0


def fixed_drives() -> list[Path]:
    """Корни встроенных дисков (C:\\, B:\\ …)."""
    if not IS_WINDOWS:
        return []
    mask = ctypes.windll.kernel32.GetLogicalDrives()
    drives = []
    for i in range(26):
        if mask & (1 << i):
            root = f"{chr(65 + i)}:\\"
            if ctypes.windll.kernel32.GetDriveTypeW(root) == 3:  # DRIVE_FIXED
                drives.append(Path(root))
    return drives


def disk_size(path: Path | str) -> int:
    """Сколько файл реально занимает на диске (с учётом сжатия)."""
    if not IS_WINDOWS:
        return os.path.getsize(path)
    high = wintypes.DWORD(0)
    low = _kernel32.GetCompressedFileSizeW(long_path(path), ctypes.byref(high))
    if low == 0xFFFFFFFF and ctypes.get_last_error() != 0:
        return os.path.getsize(long_path(path))
    return (high.value << 32) + low


def create_junction(link: Path, target: Path) -> None:
    """Точка соединения: старая папка «ведёт» в новую, программы ничего не замечают."""
    import _winapi

    _winapi.CreateJunction(str(target), str(link))


# ------------------------------------------------------------------ процессы и программы
def running_processes() -> set[str]:
    try:
        out = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True,
            encoding="oem", errors="replace", timeout=30, creationflags=NO_WINDOW,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return set()
    names = set()
    for line in out.splitlines():
        if line.startswith('"'):
            names.add(line.split('","', 1)[0].strip('"').lower())
    return names


def installed_programs() -> list[str]:
    """Названия установленных программ из раздела «Приложения» (реестр Uninstall)."""
    if winreg is None:
        return []
    places = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
    ]
    names: list[str] = []
    for hive, path in places:
        try:
            key = winreg.OpenKey(hive, path)
        except OSError:
            continue
        with key:
            i = 0
            while True:
                try:
                    sub = winreg.EnumKey(key, i)
                except OSError:
                    break
                i += 1
                try:
                    with winreg.OpenKey(key, sub) as item:
                        name, _ = winreg.QueryValueEx(item, "DisplayName")
                except OSError:
                    continue
                if isinstance(name, str) and name.strip():
                    names.append(name.strip())
    return names


def virtualbox_disks() -> dict[str, str] | None:
    """Диски, подключённые к виртуалкам VirtualBox: {путь в нижнем регистре: путь}. None — VirtualBox не найден."""
    registry = config.HOME / ".VirtualBox" / "VirtualBox.xml"
    if not registry.exists():
        return None

    def disks_in(xml_path: Path) -> tuple[dict[str, str], list[str]]:
        disks: dict[str, str] = {}
        machines: list[str] = []
        try:
            root = ET.parse(xml_path).getroot()
        except (OSError, ET.ParseError):
            return disks, machines
        for el in root.iter():
            if el.tag.endswith("HardDisk") and el.get("location"):
                location = el.get("location")
                if not os.path.isabs(location):
                    location = os.path.join(xml_path.parent, location)
                location = os.path.normpath(location)
                disks[os.path.normcase(location)] = location
            elif el.tag.endswith("MachineEntry") and el.get("src"):
                machines.append(el.get("src"))
        return disks, machines

    disks, machines = disks_in(registry)
    for machine in machines:
        disks.update(disks_in(Path(machine))[0])
    return disks


def read_zone_source(path: str) -> str:
    """Откуда скачан файл: адреса из скрытой метки Zone.Identifier, которую ставит браузер."""
    try:
        with open(long_path(path) + ":Zone.Identifier", "rb") as f:
            data = f.read(8192)
    except OSError:
        return ""
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        text = data.decode("utf-16", "ignore")
    else:
        text = data.decode("utf-8", "ignore")
    urls = []
    for line in text.splitlines():
        key, _, value = line.partition("=")
        if key.strip().lower() in ("referrerurl", "hosturl") and value.strip():
            urls.append(value.strip())
    return " ".join(urls)
