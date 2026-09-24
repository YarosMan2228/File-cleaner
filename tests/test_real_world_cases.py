"""Случаи, найденные при проверке на настоящем ноутбуке."""
import os
import time

import pytest
from conftest import write

from filecleaner import analyzers
from filecleaner.index import Index

LIB = os.urandom(150 * 1024)


@pytest.fixture(autouse=True)
def no_real_programs(monkeypatch):
    monkeypatch.setattr(analyzers, "installed_programs", lambda: [])


def check(home, rules):
    roots = {"downloads": home / "Downloads", "documents": home / "Documents", "desktop": home / "Desktop"}
    with Index() as index:
        index.scan(roots)
        result = analyzers.run_check(index, roots, rules)
    return {str(f.path.relative_to(home)).replace("\\", "/"): f for f in result.findings}


def test_files_inside_distributions_are_not_removed(sandbox, rules):
    """Одинаковые .msi в двух распакованных дистрибутивах — не «лишняя копия», только отчёт."""
    dl = sandbox / "Downloads"
    write(dl / "DistroA" / "lib" / "Setup Libset.msi", LIB)
    time.sleep(0.05)
    write(dl / "DistroB" / "lib" / "Setup Libset.msi", LIB)
    write(dl / "DistroA" / "lib" / "other.cab", os.urandom(3000))
    found = check(sandbox, rules)
    [finding] = [f for p, f in found.items() if p.endswith("Setup Libset.msi")]
    assert finding.mode == "report" and finding.rule == "duplicates.other"


def test_nested_identical_subfolders_only_reported(sandbox, rules):
    dl = sandbox / "Downloads"
    for top in ("Pack", "Pack v2"):
        for i in range(3):
            write(dl / top / "shared" / f"f{i}.bin", bytes([i]) * 5000)
    write(dl / "Pack v2" / "extra.bin", os.urandom(5000))
    found = check(sandbox, rules)
    shared = [f for p, f in found.items() if p.endswith("/shared")]
    assert shared and all(f.mode == "report" for f in shared)


def test_installers_only_loose_and_real_versions(sandbox, rules, monkeypatch):
    monkeypatch.setattr(analyzers, "installed_programs", lambda: ["Google Chrome"])
    dl = sandbox / "Downloads"
    write(dl / "tool-1.2.0-x64.msi", os.urandom(4000))
    time.sleep(0.05)
    write(dl / "tool-1.10.0-x64.msi", os.urandom(4000))      # 1.10 новее 1.2
    write(dl / "mongo-8.2.3.msi", os.urandom(4000))
    write(dl / "mongo-8.2.3 (1).msi", os.urandom(4000))       # та же версия, другая сборка — не «старая»
    write(dl / "Distro" / "Setup" / "ChromeSetup.exe", os.urandom(4000))  # внутри дистрибутива
    write(dl / "ChromeSetup.exe", os.urandom(4000))
    found = check(sandbox, rules)
    assert found["Downloads/tool-1.2.0-x64.msi"].rule == "installers.old_versions"
    assert "tool-1.10.0-x64.msi" in found["Downloads/tool-1.2.0-x64.msi"].reason
    assert found["Downloads/tool-1.10.0-x64.msi"].rule == "installers.other"
    assert found["Downloads/mongo-8.2.3.msi"].rule != "installers.old_versions"
    assert found["Downloads/ChromeSetup.exe"].rule == "installers.installed"
    assert "Downloads/Distro/Setup/ChromeSetup.exe" not in found


def test_zoom_tmp_recording_is_only_reported(sandbox, rules):
    zoom = sandbox / "Documents" / "Zoom" / "2025-12-27 Meeting"
    write(zoom / "video3588355498.mp4.tmp", os.urandom(5000))
    write(sandbox / "Documents" / "junk123.tmp", b"x" * 100)
    found = check(sandbox, rules)
    assert found["Documents/Zoom/2025-12-27 Meeting/video3588355498.mp4.tmp"].mode == "report"
    assert found["Documents/junk123.tmp"].mode == "review"


def test_empty_dirs_only_top_level(sandbox, rules):
    dl = sandbox / "Downloads"
    (dl / "BMS backup" / "bin").mkdir(parents=True)          # пустая папка внутри структуры — не трогаем
    write(dl / "BMS backup" / "project.dat", b"data")
    (dl / "New folder" / "inner").mkdir(parents=True)        # целиком пустая папка в Загрузках — можно
    found = check(sandbox, rules)
    assert "Downloads/BMS backup/bin" not in found
    assert found["Downloads/New folder"].rule == "files.empty_dirs"
    assert found["Downloads/New folder/inner"].rule == "files.empty_dirs"


def test_loose_copy_of_organized_file(sandbox, rules):
    write(sandbox / "Documents" / "Курс" / "lecture.pdf", LIB)
    time.sleep(0.05)
    write(sandbox / "Desktop" / "lecture.pdf", LIB)
    found = check(sandbox, rules)
    assert found["Desktop/lecture.pdf"].rule == "duplicates.in_downloads"
    assert "Documents/Курс/lecture.pdf" not in found


def test_architecture_is_not_a_version():
    assert analyzers._version("FACEITInstaller_64") == ()
    assert analyzers._version("python-3.12.1-amd64") == (3, 12, 1)
    assert analyzers._version("Git-2.43.0-64-bit") == (2, 43, 0)
    assert analyzers._version("tool-1.10.0-x64 (1)") == (1, 10, 0)
