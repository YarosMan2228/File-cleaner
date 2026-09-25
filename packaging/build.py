"""Сборка File Cleaner для Windows: папка с программой, переносной zip и (если есть Inno Setup) установщик.

    python packaging/build.py

Нужен PyInstaller (pip install pyinstaller). Установщик собирается, если установлен Inno Setup 6.
Готовое — в папке dist/.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACK = ROOT / "packaging"
DIST = ROOT / "dist"
BUILD = ROOT / "build"
sys.path.insert(0, str(ROOT))

from filecleaner import __version__  # noqa: E402

PUBLISHER = "Yaroslav"          # поменяй на своё имя или название компании перед продажей
PRODUCT = "File Cleaner"


def version_tuple() -> tuple[int, int, int, int]:
    parts = [int(p) for p in __version__.split(".")[:4] if p.isdigit()]
    return tuple(parts + [0] * (4 - len(parts)))  # type: ignore[return-value]


def write_version_info() -> Path:
    """Сведения о программе в свойствах файла (Проводник → Свойства → Подробно)."""
    v = version_tuple()
    text = f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={v}, prodvers={v}, mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1,
                    subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', '{PUBLISHER}'),
      StringStruct('FileDescription', '{PRODUCT} — cleans and sorts files, locally'),
      StringStruct('FileVersion', '{__version__}'),
      StringStruct('InternalName', 'filecleaner'),
      StringStruct('LegalCopyright', '© 2026 {PUBLISHER}'),
      StringStruct('OriginalFilename', '{PRODUCT}.exe'),
      StringStruct('ProductName', '{PRODUCT}'),
      StringStruct('ProductVersion', '{__version__}')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"""
    path = PACK / "version_info.txt"
    path.write_text(text, encoding="utf-8")
    return path


def find_iscc() -> Path | None:
    candidates = [
        Path(os.environ.get("ProgramFiles(x86)", "")) / "Inno Setup 6" / "ISCC.exe",
        Path(os.environ.get("ProgramFiles", "")) / "Inno Setup 6" / "ISCC.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Inno Setup 6" / "ISCC.exe",
    ]
    found = shutil.which("ISCC")
    return Path(found) if found else next((p for p in candidates if p.exists()), None)


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if not (PACK / "icon.ico").exists():
        subprocess.run([sys.executable, str(PACK / "make_icon.py")], check=True)
    write_version_info()

    print(f"Сборка {PRODUCT} {__version__}…", flush=True)
    subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--log-level", "WARN",
                    "--distpath", str(DIST), "--workpath", str(BUILD), str(PACK / "filecleaner.spec")],
                   check=True, cwd=ROOT)
    app_dir = DIST / "FileCleaner"
    for name in ("LICENSE.txt", "THIRD-PARTY-NOTICES.txt"):
        if (ROOT / name).exists():
            shutil.copyfile(ROOT / name, app_dir / name)

    portable = DIST / f"FileCleaner-{__version__}-portable"
    shutil.make_archive(str(portable), "zip", DIST, "FileCleaner")
    print(f"Папка программы: {app_dir}")
    print(f"Переносная версия: {portable}.zip")

    iscc = find_iscc()
    if iscc is None:
        print("Inno Setup 6 не найден — установщик не собран (https://jrsoftware.org/isdl.php).")
        return 0
    subprocess.run([str(iscc), "/Q", f"/DAppVersion={__version__}", f"/DPublisher={PUBLISHER}",
                    f"/O{DIST}", str(PACK / "installer.iss")], check=True, cwd=ROOT)
    print(f"Установщик: {DIST / f'FileCleaner-{__version__}-setup.exe'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
