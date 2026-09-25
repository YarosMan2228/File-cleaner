"""«Приступай»: что удаляется сразу, что ждёт утра, сверка перед удалением, защита программ."""
import os
import time
import zipfile
from pathlib import Path

from conftest import write

from filecleaner import analyzers, fsutil, night, organizer, review
from filecleaner.ai import LocalAI, is_local_url
from filecleaner.analyzers import Finding
from filecleaner.index import Index

A = os.urandom(200 * 1024)
B = os.urandom(150 * 1024)


def make_zip(path: Path, files: dict[str, bytes]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        for name, data in files.items():
            # Время внутри zip — с точностью до 2 с: без фиксированного одинаковые архивы иногда различаются.
            z.writestr(zipfile.ZipInfo(name, date_time=(2024, 1, 1, 0, 0, 0)), data)
    os.utime(path, (time.time() - 40 * 86400,) * 2)


def build(home: Path) -> None:
    docs, dl = home / "Documents", home / "Downloads"
    write(docs / "work" / "report.pdf", A)                 # оригинал
    time.sleep(0.05)
    write(dl / "report (1).pdf", A)                         # копия в свалке — удалить сразу
    write(docs / "work" / "plan.pdf", B)
    time.sleep(0.05)
    write(docs / "work" / "plan (1).pdf", B)                # копия в рабочей папке — только утром
    good = {"a.txt": b"a" * 3000, "sub/b.txt": b"b" * 3000}
    make_zip(dl / "pack.zip", good)                         # распакован целиком — удалить сразу
    for name, data in good.items():
        write(dl / "pack" / name, data)
    time.sleep(0.05)
    make_zip(dl / "pack (1).zip", good)                     # копия распакованного zip — после сверки zip
    os.utime(dl / "pack (1).zip", (time.time() - 40 * 86400,) * 2)
    make_zip(dl / "broken.zip", {"c.txt": b"c" * 3000})     # в папке тот же размер, но другое содержимое
    write(dl / "broken" / "c.txt", b"X" * 3000)
    write(dl / "notes.7z", b"7z" * 2000)                    # не zip — содержимое не сверить
    write(dl / "notes" / "n.txt", b"n" * 100)


def plan_for(home: Path, rules) -> night.NightPlan:
    roots = {"downloads": home / "Downloads", "documents": home / "Documents", "desktop": home / "Desktop"}
    with Index() as index:
        index.scan(roots)
        result = analyzers.run_check(index, roots, rules)
    night.guard_system_drive(result.findings)
    dumps = analyzers.dump_folders(roots, rules)
    auto, morning, held = night.split(result.findings, dumps)
    return night.NightPlan(roots, dumps, result, auto, morning, held)


def names(findings) -> set[str]:
    return {f.path.name for f in findings}


def test_split_deletes_only_verified_copies_in_dumps(sandbox, rules):
    build(sandbox)
    plan = plan_for(sandbox, rules)
    assert {"report (1).pdf", "pack.zip", "pack (1).zip", "broken.zip"} <= names(plan.auto)
    assert "plan (1).pdf" in names(plan.morning)                       # рабочая папка — решаешь утром
    assert "notes.7z" in names(f for f, _ in plan.held)                 # не zip — не сверить

    verified = night.verify_all(plan)
    assert {"report (1).pdf", "pack.zip", "pack (1).zip"} == names(verified)
    held = {f.path.name: why for f, why in plan.held}
    assert "не совпало" in held["broken.zip"]                            # контрольная сумма не сошлась


def test_copy_of_zip_waits_if_zip_fails_check(sandbox, rules):
    build(sandbox)
    dl = sandbox / "Downloads"
    write(dl / "pack" / "a.txt", b"Z" * 3000)                           # папка испорчена — zip не проходит сверку
    plan = plan_for(sandbox, rules)
    verified = night.verify_all(plan)
    assert "pack.zip" not in names(verified) and "pack (1).zip" not in names(verified)


def test_run_night_end_to_end(sandbox, rules, monkeypatch):
    build(sandbox)
    dl, docs = sandbox / "Downloads", sandbox / "Documents"
    write(dl / "lecture.pdf", os.urandom(4000))
    write(docs / "loose.xlsx", os.urandom(4000))
    write(docs / "Zoom" / "meeting.mp4", os.urandom(4000))           # папка программы в Документах
    roots = {"downloads": dl, "documents": docs, "desktop": sandbox / "Desktop"}
    monkeypatch.setattr(night, "night_roots", lambda r: roots)
    monkeypatch.setattr(night, "keep_awake", lambda on: None)
    monkeypatch.setattr(organizer.refs, "scan_references", lambda *a, **k: {})  # настоящий реестр не читаем

    out = night.run_night(rules, log=lambda _: None)

    assert not out.errors, out.errors
    assert not (dl / "report (1).pdf").exists() and (docs / "work" / "report.pdf").read_bytes() == A
    assert not (dl / "pack.zip").exists() and not (dl / "pack (1).zip").exists()
    assert [p.read_bytes() for p in dl.rglob("b.txt")] == [b"b" * 3000]  # содержимое zip осталось в папке
    assert out.auto_freed > 0 and out.report and out.report.exists()
    [batch] = [b for b in review.find_batches() if "проверка" in b.name]
    staged = {Path(e["original"]).name for e in batch.entries}
    assert {"plan (1).pdf", "broken.zip", "notes.7z"} <= staged        # ждут утра
    assert (docs / "work" / "plan.pdf").exists()
    assert (dl / "Учёба" / "Документы" / "lecture.pdf").exists()        # свалка разложена по секторам
    assert (docs / "Таблицы" / "loose.xlsx").exists()                   # в Документах — отдельные файлы
    assert (docs / "Zoom" / "meeting.mp4").exists()                     # а папки остаются на месте
    assert night.last_run()["report"] == str(out.report)


def test_system_drive_outside_home_is_report_only(sandbox):
    system = Path(night.config.WINDIR.drive + "\\") / "Siemens" / "copy (1).dat"
    inside = sandbox / "Downloads" / "copy (1).dat"
    findings = [
        Finding("duplicates.copy_names", "g", system, 1, "review", "копия"),
        Finding("duplicates.copy_names", "g", inside, 1, "review", "копия"),
        Finding("junk.temp", "g", Path(night.config.WINDIR) / "Temp" / "x.tmp", 1, "delete", "temp"),
    ]
    night.guard_system_drive(findings)
    assert [f.mode for f in findings] == ["report", "review", "delete"]


def test_ai_only_talks_to_this_computer(rules):
    assert is_local_url("http://localhost:11434") and is_local_url("http://127.0.0.1:9")
    assert not is_local_url("http://192.168.1.5:11434") and not is_local_url("https://ollama.example.com")
    rules.data["ai"].update(enabled=True, url="http://ollama.example.com:11434")
    ai = LocalAI(rules)
    assert not ai.available() and ai._ask("x") == {}                    # в сеть не ходит вообще


def test_sort_refuses_program_folder(sandbox, rules):
    folder = sandbox / "file_dontknowhat"
    for i in range(6):
        write(folder / f"lib{i}.dll", b"MZ")
    write(folder / "Resolve.exe", b"MZ" * 1000)
    plan = organizer.plan_sort(folder, rules, check_references=False)
    assert not plan.moves and "программы" in next(iter(plan.skipped))


def test_program_in_drive_root_is_protected(tmp_path, monkeypatch):
    root = tmp_path / "B"
    for i in range(6):
        write(root / f"lib{i}.dll", b"MZ")
    write(root / "Resolve.exe", b"MZ")
    write(root / "АБ-2" / "plan.pdf", b"%PDF")
    monkeypatch.setattr(fsutil, "is_drive_root", lambda p: fsutil.key_of(p) == fsutil.key_of(root))
    found = {entry.name: protected for entry, protected in fsutil.walk(root)}
    assert found["Resolve.exe"] is True                                 # программа в корне диска
    assert found["plan.pdf"] is False                                   # папки на диске — сами по себе
