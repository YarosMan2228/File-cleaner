"""Ночной запуск: задача в Планировщике — из XML, с нужными настройками; в тестах Планировщик поддельный."""
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from filecleaner import schedule

NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}


class FakeScheduler:
    """Вместо schtasks: помнит одну задачу по её XML."""

    def __init__(self) -> None:
        self.xml: str | None = None
        self.calls: list[str] = []

    def __call__(self, *args: str) -> subprocess.CompletedProcess:
        self.calls.append(args[0])
        if args[0] == "/Create":
            self.xml = open(args[args.index("/XML") + 1], encoding="utf-16").read()
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[0] == "/Delete":
            self.xml = None
            return subprocess.CompletedProcess(args, 0, "", "")
        if self.xml is None:
            return subprocess.CompletedProcess(args, 1, "", "ERROR: not found")
        return subprocess.CompletedProcess(args, 0, self.xml, "")


@pytest.fixture
def scheduler(monkeypatch):
    fake = FakeScheduler()
    monkeypatch.setattr(schedule, "_schtasks", fake)
    return fake


def test_task_xml_has_the_safe_settings():
    task = ET.fromstring(schedule.task_xml("03:30", wake=True).split("\n", 1)[1])
    settings = task.find("t:Settings", NS)
    assert task.find("t:Triggers/t:CalendarTrigger/t:StartBoundary", NS).text.endswith("T03:30:00")
    assert settings.find("t:WakeToRun", NS).text == "true"
    assert settings.find("t:DisallowStartIfOnBatteries", NS).text == "true"   # не от батареи
    assert settings.find("t:StartWhenAvailable", NS).text == "false"         # пропущенный запуск днём не догоняет
    assert settings.find("t:StopIfGoingOnBatteries", NS).text == "false"     # не обрывать посреди переноса
    assert task.find("t:Principals/t:Principal/t:RunLevel", NS).text == "LeastPrivilege"
    assert task.find("t:Actions/t:Exec/t:Arguments", NS).text.endswith("night --apply --yes --scheduled")


def test_apply_creates_changes_and_removes_the_task(scheduler):
    assert schedule.status()["enabled"] is False
    schedule.apply({"enabled": True, "time": "02:15", "wake": False})
    state = schedule.status()
    assert (state["enabled"], state["time"], state["wake"], state["ok"]) == (True, "02:15", False, True)

    scheduler.calls.clear()
    schedule.apply({"enabled": True, "time": "02:15", "wake": False})
    assert "/Create" not in scheduler.calls                                  # ничего не поменялось — не трогаем

    schedule.apply({"enabled": False})
    assert scheduler.calls[-1] == "/Delete" and schedule.status()["enabled"] is False
    with pytest.raises(schedule.ScheduleError):
        schedule.apply({"enabled": True, "time": "25:00"})


def test_runs_without_console(tmp_path):
    """Ночной запуск из Планировщика и «File Cleaner.exe» идут без консоли: sys.stdout и sys.stderr — None."""
    code = "import sys; sys.stdout = sys.stderr = None\nfrom filecleaner.cli import main\nsys.exit(main(['history']))"
    result = subprocess.run([sys.executable, "-c", code], cwd=Path(__file__).resolve().parents[1],
                            env={**os.environ, "FILECLEANER_HOME": str(tmp_path)}, capture_output=True, timeout=60)
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    log = (tmp_path / "logs" / "console.log").read_text(encoding="utf-8")
    assert log.count("--- ") == 1 and len(log.strip().splitlines()) == 2   # вывод без консоли — в журнале


def test_task_of_a_moved_program_is_repaired(scheduler):
    schedule.apply({"enabled": True, "time": "02:15", "wake": False})
    scheduler.xml = scheduler.xml.replace(schedule.command()[0], r"C:\Old\File Cleaner.exe")
    state = schedule.status()
    assert state["ok"] is False and state["program"] == r"C:\Old\File Cleaner.exe"
    schedule.apply({"enabled": True, "time": "02:15", "wake": False})
    assert schedule.status()["ok"] is True                   # программу перенесли — задача запускает эту копию
    with pytest.raises(schedule.ScheduleError):
        schedule.install("03:00\n", wake=True)


def test_scheduled_night_waits_for_people_and_stops_after_the_trial(sandbox, scheduler, monkeypatch):
    import datetime as dt

    from filecleaner import cli, config, licensing, night

    monkeypatch.setattr(night, "run_night", lambda *a, **k: pytest.fail("ночь не должна была начаться"))
    monkeypatch.setattr(night, "wait_for_quiet", lambda log: False)          # засиделись допоздна
    monkeypatch.setattr(cli, "_night_describe", lambda rules: None)
    assert cli.main(["night", "--apply", "--yes", "--scheduled"]) == 0      # ночь пропущена, ничего не тронуто

    schedule.apply({"enabled": True, "time": "03:00", "wake": True})
    licensing._save({"trial_started": (dt.date.today() - dt.timedelta(days=40)).isoformat()})
    assert cli.main(["night", "--apply", "--yes", "--scheduled"]) == 3
    assert schedule.status()["enabled"] is False                             # не будить компьютер впустую
    errors = (config.DATA_DIR / "logs" / "night-errors.log").read_text(encoding="utf-8")
    assert "Пробный период закончился" in errors and "Ночной запуск выключен" in errors


def test_second_operation_waits_for_the_first(sandbox, monkeypatch):
    from filecleaner import cli, winutil

    def busy():
        raise winutil.Busy("занято")

    monkeypatch.setattr(cli, "exclusive", busy)
    assert cli.main(["undo", "--yes"]) == 4                                  # окно или ночь уже работают с файлами
