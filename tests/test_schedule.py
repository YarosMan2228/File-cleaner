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
    assert task.find("t:Principals/t:Principal/t:RunLevel", NS).text == "LeastPrivilege"
    assert task.find("t:Actions/t:Exec/t:Arguments", NS).text.endswith("night --apply --yes")


def test_apply_creates_changes_and_removes_the_task(scheduler):
    assert schedule.status()["enabled"] is False
    schedule.apply({"enabled": True, "time": "02:15", "wake": False})
    assert schedule.status() == {"enabled": True, "time": "02:15", "wake": False}

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
