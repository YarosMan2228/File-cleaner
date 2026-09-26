"""«Проверить содержимое»: архив сверяется с папкой, куда его распаковали, копия — с оригиналом."""
import os
import subprocess
import tarfile
import zipfile

import pytest
from conftest import write

from filecleaner import journal, review, verify
from filecleaner.analyzers import Finding

FILES = {"a.txt": b"a" * 3000, "sub/b.txt": b"b" * 5000}


def unpacked(tmp_path, files=FILES):
    folder = tmp_path / "pack"
    for name, data in files.items():
        write(folder / name, data)
    return folder


def make_zip(path, files, prefix=""):
    with zipfile.ZipFile(path, "w") as z:
        for name, data in files.items():
            z.writestr(prefix + name, data)
    return path


def test_zip_checked_against_the_folder(tmp_path):
    folder = unpacked(tmp_path)
    archive = make_zip(tmp_path / "pack.zip", FILES)
    verdict = verify.check_archive(archive, folder)
    assert (verdict.code, verdict.ok, verdict.files) == ("ok", True, 2) and "можно удалять" in verdict.text()
    nested = make_zip(tmp_path / "nested.zip", FILES, prefix="pack/")      # в архиве папка с тем же именем
    assert verify.check_archive(nested, folder).ok is True

    write(folder / "sub" / "b.txt", b"B" * 5000)                            # файл в папке изменили
    verdict = verify.check_archive(archive, folder)
    assert (verdict.code, verdict.ok, verdict.name) == ("differs", False, "b.txt")
    os.remove(folder / "a.txt")                                            # файла в папке нет
    assert verify.check_archive(archive, folder).code == "missing"
    assert verify.check_archive(archive, tmp_path / "nowhere").code == "no_folder"


def test_other_archives_without_7zip_use_windows_tar(tmp_path, monkeypatch):
    if not os.path.exists(verify.system_exe("tar.exe")):
        pytest.skip("в этой Windows нет tar")
    monkeypatch.setattr(verify, "seven_zip", lambda: None)
    folder = unpacked(tmp_path)
    with tarfile.open(tmp_path / "pack.tar", "w") as tar:
        for name in FILES:
            tar.add(folder / name, arcname=name)
    assert verify.check_archive(tmp_path / "pack.tar", folder).ok is True   # распаковка во временную папку
    write(folder / "a.txt", b"A" * 3000)
    assert verify.check_archive(tmp_path / "pack.tar", folder).code == "differs"
    write(tmp_path / "broken.rar", b"not a rar")
    assert verify.check_archive(tmp_path / "broken.rar", folder).code == "need_7zip"


def test_7zip_listing_with_checksums(tmp_path):
    exe = verify.seven_zip()
    if exe is None:
        pytest.skip("7-Zip не установлен")
    folder = unpacked(tmp_path, {"Схема ШАК-2 (їжак).txt": b"x" * 4000, "IMG_1.jpg": b"j" * 4000})
    archive = tmp_path / "pack.7z"
    subprocess.run([str(exe), "a", str(archive), str(folder / "*")], capture_output=True, check=True)
    assert verify.check_archive(archive, folder).ok is True                 # украинские имена внутри — не помеха
    write(folder / "IMG_1.jpg", b"J" * 4000)
    assert verify.check_archive(archive, folder).code == "differs"


def test_copy_checked_against_the_original(tmp_path):
    original, copy = write(tmp_path / "report.pdf", b"r" * 4000), write(tmp_path / "report (1).pdf", b"r" * 4000)
    assert verify.check_copy(copy, original).code == "same"
    write(copy, b"R" * 4000)
    assert verify.check_copy(copy, original).ok is False
    assert verify.check_copy(copy, tmp_path / "gone.pdf").code == "no_original"


def test_verdict_is_remembered_in_the_batch(sandbox):
    dl = sandbox / "Downloads"
    folder = unpacked(dl)
    archive = make_zip(dl / "pack.zip", FILES)
    finding = Finding("archives.extracted", "Распакованные архивы", archive, archive.stat().st_size, "review",
                      "рядом папка", original=folder)
    with journal.Session("check", "Проверка") as session:
        batches, errors = review.stage([finding], session)
    assert not errors
    assert review.check_batches(batches) == {"ok": 1, "bad": 0, "unknown": 0}
    entry = review.find_batches()[0].entries[0]                            # опись перечитана с диска
    assert review.verifiable(entry) and review.verdict(entry).ok is True
