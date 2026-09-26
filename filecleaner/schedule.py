"""«Приступай» каждую ночь: задача в Планировщике Windows (для текущего пользователя, без прав администратора).

Задача создаётся из XML — только так можно сказать «будить компьютер», «не запускаться от батареи»
и «не догонять пропущенный запуск днём» (иначе файлы начали бы переезжать, пока ты работаешь).
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from xml.sax.saxutils import escape, unescape

from .i18n import tr
from .winutil import NO_WINDOW, system_exe

TASK_NAME = "File Cleaner - Night"
_TIME = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class ScheduleError(RuntimeError):
    pass


def command() -> tuple[str, str, str]:
    """(программа, аргументы, рабочая папка) для ночного запуска — без окна консоли."""
    if getattr(sys, "frozen", False):  # установленная программа: «File Cleaner.exe» без консоли
        exe = Path(sys.executable).with_name("File Cleaner.exe")
        return str(exe), "night --apply --yes --scheduled", str(exe.parent)
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    python = pythonw if pythonw.exists() else Path(sys.executable)
    return str(python), "-m filecleaner night --apply --yes --scheduled", str(Path(__file__).resolve().parents[1])


def task_xml(at: str, wake: bool) -> str:
    program, arguments, workdir = command()
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>{escape(tr("File Cleaner: чистка и сортировка каждую ночь («Приступай»)."))}</Description>
  </RegistrationInfo>
  <Triggers>
    <CalendarTrigger>
      <StartBoundary>2026-01-01T{at}:00</StartBoundary>
      <Enabled>true</Enabled>
      <ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay>
    </CalendarTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author"><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>true</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>false</StartWhenAvailable>
    <WakeToRun>{"true" if wake else "false"}</WakeToRun>
    <ExecutionTimeLimit>PT12H</ExecutionTimeLimit>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(program)}</Command>
      <Arguments>{escape(arguments)}</Arguments>
      <WorkingDirectory>{escape(workdir)}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def _schtasks(*args: str) -> subprocess.CompletedProcess:
    try:
        return subprocess.run([system_exe("schtasks.exe"), *args], capture_output=True, text=True, encoding="oem",
                              errors="replace", timeout=30, creationflags=NO_WINDOW)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ScheduleError(tr("Планировщик Windows не ответил: {error}", error=exc)) from exc


def status() -> dict:
    """{"enabled": есть ли задача (None — Планировщик не ответил), "time": "03:00", "wake": будит ли компьютер,
    "ok": запускает ли она эту копию программы, "program": что запускает}."""
    off = {"enabled": False, "time": "03:00", "wake": True, "ok": True, "program": ""}
    try:
        result = _schtasks("/Query", "/TN", TASK_NAME, "/XML")
    except ScheduleError:
        return {**off, "enabled": None}
    if result.returncode != 0:
        return off
    xml = result.stdout
    time = re.search(r"<StartBoundary>[^<]*T(\d{2}:\d{2})", xml)
    settings = re.search(r"<Settings>(.*?)</Settings>", xml, re.S)
    disabled = bool(settings and "<Enabled>false</Enabled>" in settings.group(1))  # выключена вручную
    found = re.search(r"<Command>([^<]*)</Command>", xml)
    program = unescape(found.group(1)).strip('"') if found else ""
    here = command()[0]
    ok = os.path.normcase(os.path.abspath(program)) == os.path.normcase(os.path.abspath(here)) and os.path.exists(here)
    return {"enabled": not disabled, "time": time.group(1) if time else "03:00",
            "wake": "<WakeToRun>true</WakeToRun>" in xml, "ok": ok, "program": program}


def install(at: str, wake: bool) -> None:
    if not _TIME.fullmatch(at):
        raise ScheduleError(tr("Время — в виде 03:00."))
    fd, path = tempfile.mkstemp(suffix=".xml")
    try:
        with os.fdopen(fd, "w", encoding="utf-16") as fh:  # Планировщик читает XML только в UTF-16
            fh.write(task_xml(at, wake))
        result = _schtasks("/Create", "/TN", TASK_NAME, "/XML", path, "/F")
    finally:
        os.unlink(path)
    if result.returncode != 0:
        raise ScheduleError(tr("Планировщик Windows не принял задачу: {error}",
                               error=(result.stderr or result.stdout).strip()))


def remove() -> None:
    result = _schtasks("/Delete", "/TN", TASK_NAME, "/F")
    if result.returncode != 0 and status()["enabled"] is not False:
        raise ScheduleError(tr("Не получилось убрать задачу из Планировщика: {error}",
                               error=(result.stderr or result.stdout).strip()))


def apply(wanted: dict) -> None:
    """Приводит задачу к нужному виду: включить/выключить, время, «будить компьютер»."""
    current = status()
    if not wanted.get("enabled"):
        if current["enabled"] is not False:
            remove()
        return
    at, wake = str(wanted.get("time", "03:00")), bool(wanted.get("wake", True))
    if (current["enabled"], current["time"], current["wake"], current["ok"]) != (True, at, wake, True):
        install(at, wake)  # в том числе если задача запускала другую копию программы (перенесли, переустановили)
