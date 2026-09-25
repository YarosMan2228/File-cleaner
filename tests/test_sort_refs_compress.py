"""Сортировка по секторам, ссылки для программ, сжатие и их отмена."""
import os

import pytest
from conftest import write

from filecleaner import compress, journal, organizer, refs
from filecleaner.fsutil import path_is_link
from filecleaner.winutil import disk_size


def test_sort_plan_apply_and_undo(sandbox, rules):
    dl = sandbox / "Downloads"
    write(dl / "IMG_20240101_1200.jpg", b"jpg" * 100)
    write(dl / "Screenshot 2024-05-01 101010.png", b"png" * 100)
    write(dl / "lab3 report.pdf", b"pdf" * 100)
    write(dl / "notes.txt", b"txt" * 100)
    write(dl / "data.xyz", b"???")
    write(dl / "shortcut.lnk", b"lnk")
    write(dl / "Desigo CC ClickOnce.appref-ms", b"app")
    write(dl / "Документы" / "same.pdf", b"same" * 100)
    write(dl / "same.pdf", b"same" * 100)
    write(dl / "DCC_V9_setup" / "setup.exe", b"exe" * 100)
    write(dl / "DCC_V9_setup" / "readme.txt", b"txt")
    write(dl / "Movies" / "a.mp4", b"v" * 10_000)
    write(dl / "Movies" / "a.srt", b"s")
    write(dl / "Telegram Desktop" / "x.pdf", b"t")

    plan = organizer.plan_sort(dl, rules, check_references=False)
    where = {m.src.name: m for m in plan.moves}
    rel = lambda m: m.dst.relative_to(dl).as_posix()  # noqa: E731

    assert rel(where["IMG_20240101_1200.jpg"]) == "Изображения/Фото/IMG_20240101_1200.jpg"
    assert rel(where["Screenshot 2024-05-01 101010.png"]).startswith("Изображения/Скриншоты/")
    assert rel(where["lab3 report.pdf"]) == "Учёба/Документы/lab3 report.pdf"
    assert rel(where["notes.txt"]) == "Документы/notes.txt"
    assert rel(where["DCC_V9_setup"]) == "Работа/DCC_V9_setup" and where["DCC_V9_setup"].is_dir
    assert rel(where["Movies"]) == "Видео/Movies"
    assert where["same.pdf"].duplicate_of == dl / "Документы" / "same.pdf"
    assert "data.xyz" not in where and dl / "data.xyz" in plan.unknown
    assert "shortcut.lnk" not in where and "Telegram Desktop" not in where
    assert "Desigo CC ClickOnce.appref-ms" not in where

    with journal.Session("sort", "Сортировка") as session:
        result = organizer.apply_sort(plan, rules, session)
    assert result.moved == len(plan.moves) - 1 and result.duplicates_staged == 1
    assert (dl / "Учёба" / "Документы" / "lab3 report.pdf").exists()
    assert (dl / "Работа" / "DCC_V9_setup" / "setup.exe").exists()
    assert not (dl / "same.pdf").exists()                   # дубликат уехал на проверку

    [info] = [s for s in journal.list_sessions() if s.undoable]
    journal.undo(info)
    for name in ("IMG_20240101_1200.jpg", "lab3 report.pdf", "notes.txt", "same.pdf", "DCC_V9_setup", "Movies"):
        assert (dl / name).exists(), name
    assert not (dl / "Учёба").exists() and not (dl / "Изображения").exists()   # созданные папки убраны
    assert (dl / "Документы" / "same.pdf").exists()                            # чужое не тронуто


def test_referenced_file_keeps_link(sandbox, rules):
    dl = sandbox / "Downloads"
    write(dl / "project.mp4", b"video" * 100)
    plan = organizer.plan_sort(dl, rules, check_references=False)
    [move] = plan.moves
    move.referenced_by = {"CapCut"}                          # программа помнит этот файл
    with journal.Session("sort", "Сортировка") as session:
        result = organizer.apply_sort(plan, rules, session)
    old = dl / "project.mp4"
    assert result.links == 1 and path_is_link(old)
    assert old.read_bytes() == b"video" * 100               # по старому пути файл открывается
    journal.undo(journal.list_sessions()[0])
    assert old.exists() and not path_is_link(old)


def test_folder_junction(sandbox, tmp_path):
    from filecleaner.winutil import create_junction

    target = tmp_path / "target"
    write(target / "f.txt", "ok")
    link = tmp_path / "link"
    create_junction(link, target)
    assert (link / "f.txt").read_text() == "ok" and path_is_link(link)
    refs.remove_link(link, True)
    assert not link.exists() and (target / "f.txt").exists()


def test_extract_and_rewrite_paths():
    prefix = r"C:\Users\Me\Downloads"
    text = (r'{"a": "C:\\Users\\Me\\Downloads\\x y.pdf", "b": "c:/users/me/downloads/sub/z.txt",'
            r' "c": "C:\Users\Me\Downloads2\no.txt"}' + "\n[F00000000]*C:\\Users\\Me\\Downloads\\doc.docx")
    paths = [p for _, _, p in refs.extract_paths(text, [prefix])]
    assert r"C:\Users\Me\Downloads\x y.pdf" in paths
    assert r"c:\users\me\downloads\sub\z.txt" in paths
    assert r"C:\Users\Me\Downloads\doc.docx" in paths
    assert not any("Downloads2" in p for p in paths)

    moves = refs.MoveMap()
    moves.add(r"C:\Users\Me\Downloads\doc.docx", r"C:\Users\Me\Downloads\Документы\doc.docx", False)
    moves.add(r"C:\Users\Me\Downloads\sub", r"D:\Архив\sub", True)
    new = moves.rewrite(text, [prefix])
    assert r"*C:\Users\Me\Downloads\Документы\doc.docx" in new
    assert "D:/Архив/sub/z.txt" in new
    assert r"C:\\Users\\Me\\Downloads\\x y.pdf" in new        # не перемещался — не тронут


@pytest.mark.skipif(os.name != "nt", reason="прозрачное сжатие есть только в Windows")
def test_compress_and_undo(sandbox, rules):
    folder = sandbox / "Documents" / "cold"
    text = write(folder / "log.txt", "одна и та же строка лога\n" * 50_000)
    write(folder / "photo.jpg", os.urandom(100_000))
    before = disk_size(text)
    plan = compress.plan_compress(folder, rules)
    assert [c.path.name for c in plan.candidates] == ["log.txt"]
    with journal.Session("compress", "Сжатие") as session:
        result = compress.apply_compress(plan, rules, session)
    assert result.saved > before // 2 and disk_size(text) < before // 2
    assert text.read_text(encoding="utf-8").startswith("одна и та же")
    journal.undo(journal.list_sessions()[0])
    assert disk_size(text) == before


def test_rules_forbid_deleting_user_files(tmp_path):
    from filecleaner.rules import Rules, RulesError

    path = tmp_path / "rules.toml"
    path.write_text('[duplicates]\ncopy_names = "delete"\n', encoding="utf-8")
    with pytest.raises(RulesError):
        Rules.load(path)


def test_copy_names_and_tokens():
    from filecleaner.analyzers import looks_like_copy
    from filecleaner.fsutil import keyword_match, name_tokens, parse_size

    assert looks_like_copy("отчёт (1)") and looks_like_copy("отчёт — копия") and looks_like_copy("Copy of x")
    assert not looks_like_copy("photocopy") and not looks_like_copy("отчёт")
    assert name_tokens("VSCodeUserSetup-x64_1.85") == ["vs", "code", "user", "setup", "x", "64", "1", "85"]
    assert keyword_match(name_tokens("Lab3 report"), "lab") and not keyword_match(name_tokens("label"), "lab")
    assert keyword_match(name_tokens("Лекция 5"), "лекц")
    assert parse_size("100KB") == 102400 and parse_size("1,5 ГБ") == int(1.5 * 1024**3)


def test_protected_files_and_folders_are_not_sorted(sandbox, rules):
    dl = sandbox / "Downloads"
    write(dl / "dbms_pw2_share.vbox", b"vm" * 100)
    write(dl / "PW2_Report.docx", b"doc" * 100)
    write(dl / "Course VM" / "dbms_pw2.vdi", b"disk" * 100)   # защищённый файл внутри папки
    write(dl / "B" / "keep.pdf", b"pdf" * 100)
    write(dl / "notes.txt", b"txt" * 100)
    write(dl / "Siemens contract.pdf", b"pdf" * 100)          # «contract» — от удаления, но не от сортировки
    rules.data["protect"]["keep_keywords"] = ["pw2"]
    rules.data["protect"]["paths"] = [str(dl / "B")]

    plan = organizer.plan_sort(dl, rules, check_references=False)
    where = {m.src.name: m.label for m in plan.moves}
    assert where == {"notes.txt": "Документы", "Siemens contract.pdf": "Работа / Документы"}
    assert plan.skipped[organizer.PROTECTED] == 4              # pw2, папка с pw2 и путь из [protect] — на месте


def test_multi_part_keyword_matches_consecutive_words():
    from filecleaner.fsutil import keyword_match, name_tokens

    assert keyword_match(name_tokens("dbms_pw2_share.vbox"), "pw2")
    assert keyword_match(name_tokens("DKN_AB2_ШАК12.xlsx"), "ab2")
    assert not keyword_match(name_tokens("pw20_notes.txt"), "pw2")
    assert not keyword_match(name_tokens("2_pw.txt"), "pw2")
    assert keyword_match(name_tokens("Kyivstar_BMS.zip"), "kyiv")    # одно слово — по-прежнему и как начало


def test_davinci_cache_is_not_walked(tmp_path):
    from filecleaner.fsutil import walk

    write(tmp_path / "CacheClip" / "a" / "0001.dvcc", b"x")
    write(tmp_path / "Video" / "clip.mp4", b"x")
    assert [e.name for e, _ in walk(tmp_path)] == ["clip.mp4"]
