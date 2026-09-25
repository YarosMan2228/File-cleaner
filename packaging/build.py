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


def signing_env() -> dict[str, str] | None:
    """Сертификат для подписи из окружения — или None (тогда файлы не подписываются).

    FILECLEANER_SIGN_PFX (+ FILECLEANER_SIGN_PASSWORD) — файл сертификата;
    FILECLEANER_SIGN_THUMBPRINT — отпечаток сертификата в хранилище «Личное» (CurrentUser\\My);
    FILECLEANER_SIGN_TIMESTAMP — сервер меток времени (по умолчанию DigiCert; пусто — без метки).
    Пароль передаётся через окружение, а не в командной строке.
    """
    pfx, thumb = os.environ.get("FILECLEANER_SIGN_PFX", ""), os.environ.get("FILECLEANER_SIGN_THUMBPRINT", "")
    if not pfx and not thumb:
        return None
    return {**os.environ, "FC_PFX": pfx, "FC_PASSWORD": os.environ.get("FILECLEANER_SIGN_PASSWORD", ""),
            "FC_THUMB": thumb, "FC_TIMESTAMP": os.environ.get("FILECLEANER_SIGN_TIMESTAMP", "http://timestamp.digicert.com")}


SIGN_CMD = f'powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "{PACK / "sign.ps1"}"'


def sign(files: list[Path], env: dict[str, str]) -> None:
    subprocess.run(f"{SIGN_CMD} " + " ".join(f'"{f}"' for f in files), check=True, env=env)


def third_party_notices() -> str:
    """Лицензии того, что входит в сборку, — из самих файлов лицензий установленных пакетов."""
    import importlib.metadata as md

    sections = [("Python", "https://www.python.org/", Path(sys.base_prefix) / "LICENSE.txt")]
    for dist_name, title, url in (("pypdf", "pypdf", "https://github.com/py-pdf/pypdf"),
                                  ("pyinstaller", "PyInstaller bootloader", "https://pyinstaller.org/")):
        try:
            dist = md.distribution(dist_name)
        except md.PackageNotFoundError:
            continue
        files = [f for f in dist.files or [] if Path(str(f)).name.upper().startswith(("LICENSE", "COPYING"))]
        if files:
            sections.append((f"{title} {dist.version}", url, Path(dist.locate_file(files[0]))))
    parts = ["File Cleaner includes the following third-party software. Their licenses follow.\n"]
    for title, url, path in sections:
        if path.exists():
            parts.append(f"\n{'=' * 78}\n{title} — {url}\n{'=' * 78}\n\n{path.read_text(encoding='utf-8', errors='replace')}")
    return "".join(parts)


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
    if (ROOT / "LICENSE.txt").exists():
        shutil.copyfile(ROOT / "LICENSE.txt", app_dir / "LICENSE.txt")
    (app_dir / "THIRD-PARTY-NOTICES.txt").write_text(third_party_notices(), encoding="utf-8")

    env = signing_env()
    if env is None:
        print("Подпись: сертификат не задан — файлы не подписаны (как подписать — docs/SIGNING.md).")
    else:
        sign([app_dir / "File Cleaner.exe", app_dir / "filecleaner.exe"], env)

    portable = DIST / f"FileCleaner-{__version__}-portable"
    shutil.make_archive(str(portable), "zip", DIST, "FileCleaner")
    print(f"Папка программы: {app_dir}")
    print(f"Переносная версия: {portable}.zip")

    iscc = find_iscc()
    if iscc is None:
        print("Inno Setup 6 не найден — установщик не собран (https://jrsoftware.org/isdl.php).")
        return 0
    args = [str(iscc), "/Q", f"/DAppVersion={__version__}", f"/DPublisher={PUBLISHER}", f"/O{DIST}"]
    if env is not None:  # Inno Setup подпишет и установщик, и деинсталлятор тем же скриптом
        args += ["/DSIGN", "/Sfcsign=powershell.exe $p"]  # параметры (путь к sign.ps1) — в installer.iss
    subprocess.run([*args, str(PACK / "installer.iss")], check=True, cwd=ROOT, env=env)
    print(f"Установщик: {DIST / f'FileCleaner-{__version__}-setup.exe'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
