"""Выпуск на GitHub: тег v<версия> и файлы из dist/ — установщик и переносной zip.

    python packaging/release.py            # проверить, что всё готово, и показать, что будет сделано
    python packaging/release.py --publish  # выпустить

Нужен GitHub CLI: winget install GitHub.cli, потом gh auth login.
После выпуска программы у пользователей покажут «Вышла версия …» (проверка раз в день),
а в подсказке — раздел этой версии из CHANGELOG.md.
"""
from __future__ import annotations

import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"
sys.path.insert(0, str(ROOT))

from filecleaner import __version__, licensing  # noqa: E402
from filecleaner.update import REPO  # noqa: E402


def run(*args: str) -> str:
    return subprocess.run(args, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace").stdout.strip()


def notes() -> str:
    """Раздел «## <версия>» из CHANGELOG.md — он же «что нового» у пользователей."""
    path = ROOT / "CHANGELOG.md"
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    found = re.search(rf"^## {re.escape(__version__)}\b.*?$(.*?)(?=^## |\Z)", text, re.M | re.S)
    return found.group(1).strip() if found else ""


def signed(path: Path) -> bool:
    status = run("powershell", "-NoProfile", "-Command", f"(Get-AuthenticodeSignature -LiteralPath '{path}').Status")
    return status == "Valid"


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    tag = f"v{__version__}"
    files = [DIST / f"FileCleaner-{__version__}-setup.exe", DIST / f"FileCleaner-{__version__}-portable.zip"]
    problems = [f"нет {f.relative_to(ROOT)} — сначала python packaging/build.py" for f in files if not f.exists()]
    if run("git", "status", "--porcelain", "--untracked-files=no"):
        problems.append("есть незакоммиченные изменения")
    if run("git", "rev-parse", "HEAD") != run("git", "rev-parse", "@{u}"):
        problems.append("последний коммит не отправлен на GitHub (git push)")
    if run("git", "tag", "--list", tag):
        problems.append(f"тег {tag} уже есть — подними __version__ в filecleaner/__init__.py")
    text = notes()
    if not text:
        problems.append(f"в CHANGELOG.md нет раздела «## {__version__}» — что нового в этой версии")
    for problem in problems:
        print("✗", problem)
    if problems:
        return 1
    if not signed(files[0]):
        print("! Установщик не подписан: Windows SmartScreen будет предупреждать (docs/SIGNING.md).")
    if not licensing.BUY_URL:
        print("! Нет ссылки на магазин (BUY_URL в filecleaner/licensing.py): после пробного периода купить будет негде.")

    command = ["gh", "release", "create", tag, *map(str, files), "--repo", REPO, "--title", f"File Cleaner {__version__}"]
    if REPO.lower() in run("git", "remote", "get-url", "origin").lower():  # выпуски — в репозитории с исходниками
        command += ["--target", run("git", "rev-parse", "HEAD")]
    print("Что нового:\n" + text + "\n")
    print("Будет выполнено:", " ".join(command[:4]), "… --notes-file <CHANGELOG>")
    if "--publish" not in sys.argv:
        print("Это проверка. Выпустить: python packaging/release.py --publish")
        return 0
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as fh:
        fh.write(text)
    try:
        return subprocess.run([*command, "--notes-file", fh.name], cwd=ROOT).returncode
    finally:
        Path(fh.name).unlink(missing_ok=True)


if __name__ == "__main__":
    sys.exit(main())
