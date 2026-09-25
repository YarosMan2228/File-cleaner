"""Проверка → «Ready for approval» → утверждение / отмена на игрушечной домашней папке."""
import os
import time
import zipfile

from conftest import write

from filecleaner import analyzers, config, journal, pipeline, review
from filecleaner.index import Index

A = os.urandom(200 * 1024)


def build(home):
    docs, dl = home / "Documents", home / "Downloads"
    write(docs / "work" / "report.pdf", A)          # оригинал — создан первым
    time.sleep(0.05)
    write(dl / "report.pdf", A)                      # лишняя копия в Загрузках
    write(dl / "report (1).pdf", A)                  # имя-копия
    write(docs / "other" / "report.pdf", A)          # копия в другой «осмысленной» папке — только отчёт
    write(dl / "unique.bin", os.urandom(200 * 1024))
    for folder in ("Proj", "Proj (2)"):              # одинаковые папки целиком
        for i in range(3):
            write(dl / folder / f"part{i}.dat", bytes([i]) * 2048)
    hard = write(docs / "hard.bin", os.urandom(50 * 1024))
    os.link(hard, docs / "hard-link.bin")            # жёсткая ссылка — не дубликат
    write(dl / "Thumbs.db", b"x" * 100)
    write(dl / "~$doc.docx", b"x" * 162)
    write(dl / "movie.mp4.crdownload", b"x" * 5000)
    (dl / "empty" / "nested").mkdir(parents=True)
    with zipfile.ZipFile(dl / "pack.zip", "w") as archive:
        archive.writestr("a.txt", "a" * 3000)
        archive.writestr("b.txt", "b" * 3000)
    write(dl / "pack" / "a.txt", "a" * 3000)
    write(dl / "pack" / "b.txt", "b" * 3000)
    os.utime(dl / "pack.zip", (time.time() - 40 * 86400,) * 2)


def check(home, rules):
    roots = {"downloads": home / "Downloads", "documents": home / "Documents"}
    with Index() as index:
        index.scan(roots)
        return analyzers.run_check(index, roots, rules)


def test_check_finds_everything_with_reasons(sandbox, rules):
    build(sandbox)
    result = check(sandbox, rules)
    found = {str(f.path.relative_to(sandbox)).replace("\\", "/"): f for f in result.findings}

    assert found["Downloads/report.pdf"].rule == "duplicates.in_downloads"
    assert found["Downloads/report.pdf"].original == sandbox / "Documents" / "work" / "report.pdf"
    assert found["Downloads/report (1).pdf"].rule == "duplicates.copy_names"
    assert found["Documents/other/report.pdf"].mode == "report"          # разные папки — решаешь сам
    assert found["Downloads/Proj (2)"].rule == "duplicates.folders"
    assert found["Downloads/Proj (2)"].is_dir
    assert found["Downloads/Thumbs.db"].mode == "delete"
    assert found["Downloads/~$doc.docx"].rule == "files.office_locks"
    assert found["Downloads/movie.mp4.crdownload"].rule == "files.partial_downloads"
    assert found["Downloads/pack.zip"].rule == "archives.extracted"
    assert "все файлы на месте" in found["Downloads/pack.zip"].reason
    assert found["Downloads/empty"].rule == "files.empty_dirs"
    # не должно быть: оригинал, уникальный файл, жёсткая ссылка, файлы внутри найденной папки
    for path in ("Documents/work/report.pdf", "Downloads/unique.bin", "Documents/hard.bin",
                 "Documents/hard-link.bin", "Downloads/Proj", "Downloads/Proj (2)/part0.dat"):
        assert path not in found, path


def test_apply_then_approve_with_return(sandbox, rules):
    build(sandbox)
    dl = sandbox / "Downloads"
    result = check(sandbox, rules)
    with journal.Session("check", "Проверка") as session:
        out = pipeline.apply_check(result, session)

    assert not (dl / "Thumbs.db").exists() and not (dl / "empty").exists()   # удалено сразу
    assert not (dl / "report (1).pdf").exists() and not (dl / "Proj (2)").exists()  # уехало на проверку
    assert (sandbox / "Documents" / "other" / "report.pdf").exists()          # «только отчёт» не тронут
    [batch] = out.batches
    assert (batch.path / review.REPORT).exists() and (batch.path / config.RETURN_DIR_NAME).is_dir()

    # Пользователь передумал насчёт одного файла и перетащил его в «_ВЕРНУТЬ».
    entry = next(e for e in batch.entries if e["original"].endswith("report (1).pdf"))
    staged = batch.path / entry["staged"]
    staged.rename(batch.path / config.RETURN_DIR_NAME / staged.name)

    [batch] = review.find_batches()
    with journal.Session("approve", "Утверждение") as session:
        res = review.approve(batch, session)

    assert (dl / "report (1).pdf").read_bytes() == A          # вернулся на место
    assert res.deleted == len(batch.entries) - 1
    assert not (dl / "report.pdf").exists() and not (dl / "pack.zip").exists()
    assert (dl / "pack" / "a.txt").exists() and (dl / "Proj").exists()   # оригиналы на месте
    assert not batch.path.exists()                                        # партия убрана целиком
    assert review.find_batches() == []


def test_undo_check_returns_files(sandbox, rules):
    build(sandbox)
    dl = sandbox / "Downloads"
    result = check(sandbox, rules)
    with journal.Session("check", "Проверка") as session:
        out = pipeline.apply_check(result, session)
    batch_path = out.batches[0].path

    [info] = [s for s in journal.list_sessions() if s.undoable]
    res = journal.undo(info)

    assert res.irreversible == 1                       # Thumbs.db не вернуть — это нормально
    assert (dl / "report (1).pdf").exists() and (dl / "Proj (2)" / "part1.dat").exists()
    assert (dl / "~$doc.docx").exists() and (dl / "empty").is_dir()
    assert not batch_path.exists()
    assert journal.list_sessions()[0].undone


def test_unmatched_return_is_kept(sandbox, rules):
    build(sandbox)
    result = check(sandbox, rules)
    with journal.Session("check", "Проверка") as session:
        [batch] = pipeline.apply_check(result, session).batches
    stranger = write(batch.path / config.RETURN_DIR_NAME / "моё.txt", "не из этой партии")
    with journal.Session("approve", "Утверждение") as session:
        res = review.approve(batch, session)
    assert stranger.exists()                            # чужое не удаляем
    assert res.unmatched and "моё.txt" in res.unmatched[0]
    assert review.find_batches() == []                  # партия закрыта


def test_protect_keywords_only_report(sandbox, rules):
    write(sandbox / "Documents" / "keep" / "Паспорт скан.pdf", A)
    time.sleep(0.05)
    write(sandbox / "Downloads" / "Паспорт скан.pdf", A)
    result = check(sandbox, rules)
    [finding] = [f for f in result.findings if f.path.parent.name == "Downloads"]
    assert finding.mode == "report" and "паспорт" in finding.reason


def test_keep_keywords_protect_from_deletion_too(sandbox, rules):
    write(sandbox / "Documents" / "keep" / "PW2_Report.docx", A)
    time.sleep(0.05)
    write(sandbox / "Downloads" / "PW2_Report.docx", A)
    rules.data["protect"]["keep_keywords"] = ["pw2"]
    result = check(sandbox, rules)
    [finding] = [f for f in result.findings if f.path.parent.name == "Downloads"]
    assert finding.mode == "report" and "pw2" in finding.reason


def test_busy_temp_files_are_counted_not_errors(sandbox, rules, monkeypatch):
    from filecleaner.analyzers import CheckResult, Finding

    temp = write(sandbox / "Temp" / "locked.tmp", b"x" * 10)
    copy = write(sandbox / "Downloads" / "copy.pdf", b"y" * 10)
    findings = [Finding("junk.temp", "Временные файлы", temp, 10, "delete", "временный файл"),
                Finding("duplicates.same_folder", "Копии", copy, 10, "delete", "копия")]

    def deny(self, path, *args, **kwargs):
        raise PermissionError(13, "Access is denied")

    monkeypatch.setattr(journal.Session, "delete", deny)
    with journal.Session("check", "Проверка") as session:
        out = pipeline.apply_check(CheckResult(findings, [], []), session)
    assert out.busy == 1 and temp.exists()                      # занятый кэш — не ошибка, просто пропущен
    assert len(out.errors) == 1 and "copy.pdf" in out.errors[0]  # а сбой с твоим файлом — ошибка
