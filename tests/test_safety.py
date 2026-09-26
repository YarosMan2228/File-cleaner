"""Ночь без сюрпризов: одна операция за раз, запуск по расписанию не мешает человеку, перенос не портит файлы."""
import errno
import os
import shutil
import threading

import pytest

from filecleaner import fsutil, night, winutil


def test_one_operation_at_a_time():
    """Окно, консоль и Планировщик — разные процессы; замок общий на весь сеанс Windows."""
    busy = []

    def other() -> None:
        try:
            with winutil.exclusive("Local\\FileCleaner-test"):
                pass
        except winutil.Busy as exc:
            busy.append(exc)

    with winutil.exclusive("Local\\FileCleaner-test"):
        worker = threading.Thread(target=other)
        worker.start()
        worker.join()
    assert len(busy) == 1                                    # пока идёт первая — вторая не начинается
    worker = threading.Thread(target=other)
    worker.start()
    worker.join()
    assert len(busy) == 1                                    # первая закончилась — можно


def test_scheduled_run_waits_until_nobody_is_at_the_computer(monkeypatch):
    monkeypatch.setattr(night.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(night, "keep_awake", lambda on: None)
    idle = iter([5, 100, 700])                               # работают… работают… ушли
    monkeypatch.setattr(night, "idle_seconds", lambda: next(idle))
    said: list[str] = []
    assert night.wait_for_quiet(said.append, need=600, limit=3600)
    assert len(said) == 1                                    # «начну, когда никого не будет» — один раз

    monkeypatch.setattr(night, "idle_seconds", lambda: 5)
    assert not night.wait_for_quiet(said.append, need=600, limit=0)   # так и сидят — ночь пропускается


def test_no_sleep_while_someone_is_at_the_computer(monkeypatch):
    slept = []
    monkeypatch.setattr(night, "go_to_sleep", lambda: slept.append(1) or True)
    monkeypatch.setattr(night.time, "sleep", lambda seconds: None)
    quiet = lambda text: None  # noqa: E731

    monkeypatch.setattr(night, "idle_seconds", lambda: 30)            # трогали полминуты назад
    night.after("sleep", quiet, delay=30, scheduled=True)
    assert not slept

    idle = iter([10_000, 10_000, 3])                                   # ушёл, но во время отсчёта вернулся
    monkeypatch.setattr(night, "idle_seconds", lambda: next(idle))
    night.after("sleep", quiet, delay=30, scheduled=True)
    assert not slept

    monkeypatch.setattr(night, "idle_seconds", lambda: 10_000)
    night.after("sleep", quiet, delay=30, scheduled=True)
    assert slept == [1]


@pytest.fixture
def other_drive(monkeypatch):
    """Как будто цель на другом диске: простое переименование не проходит, только копия."""
    real = os.rename

    def rename(src, dst):
        if not str(src).endswith(".fc-part"):
            raise OSError(errno.EXDEV, "другой диск")
        return real(src, dst)

    monkeypatch.setattr(fsutil.os, "rename", rename)


def test_move_to_another_drive(tmp_path, other_drive):
    out = tmp_path / "out"
    out.mkdir()
    (tmp_path / "a.txt").write_text("A")
    fsutil.move_path(tmp_path / "a.txt", out / "a.txt")
    (tmp_path / "F" / "sub").mkdir(parents=True)
    (tmp_path / "F" / "sub" / "x.txt").write_text("x")
    fsutil.move_path(tmp_path / "F", out / "F")
    assert (out / "a.txt").read_text() == "A" and not (tmp_path / "a.txt").exists()
    assert (out / "F" / "sub" / "x.txt").read_text() == "x" and not (tmp_path / "F").exists()
    assert not list(out.glob("*.fc-part*"))                  # временных копий не осталось


def test_move_never_overwrites_what_appeared_meanwhile(tmp_path, other_drive, monkeypatch):
    source, target = tmp_path / "b.txt", tmp_path / "out" / "b.txt"
    source.write_text("B")
    target.parent.mkdir()
    real_copy = shutil.copy2

    def copy2(src, dst, **kwargs):
        target.write_text("other")                           # другой процесс успел положить файл с тем же именем
        return real_copy(src, dst, **kwargs)

    monkeypatch.setattr(fsutil.shutil, "copy2", copy2)
    with pytest.raises(OSError):
        fsutil.move_path(source, target)
    assert target.read_text() == "other" and source.read_text() == "B"   # ничего не перезаписано и не потеряно
    assert not list(target.parent.glob("*.fc-part*"))
