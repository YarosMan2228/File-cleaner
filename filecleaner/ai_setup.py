"""Первый запуск ИИ: установлена ли Ollama, запущена ли, скачана ли модель — и помощь с каждым шагом.

Всё общение — только с Ollama на этом компьютере (ai.is_local_url); установщики программа сама не
скачивает и не запускает — для Ollama открывается её официальный сайт.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path

from .ai import _OPENER, LocalAI
from .i18n import tr
from .rules import Rules

OLLAMA_SITE = "https://ollama.com/download"


def ollama_app() -> Path | None:
    """Приложение Ollama (значок в трее — оно же запускает сервер)."""
    candidates = [
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama app.exe",
        Path(os.environ.get("ProgramFiles", "")) / "Ollama" / "ollama app.exe",
    ]
    return next((p for p in candidates if p.is_file()), None)


def _models(url: str) -> list[str] | None:
    """Скачанные модели; None — Ollama не отвечает."""
    try:
        with _OPENER.open(url + "/api/tags", timeout=3) as resp:
            return [m.get("name", "") for m in json.loads(resp.read()).get("models", [])]
    except (OSError, ValueError, urllib.error.URLError):
        return None


def has_model(models: list[str], model: str) -> bool:
    return any(m == model or m.split(":")[0] == model or m == f"{model}:latest" for m in models)


def status(rules: Rules) -> dict:
    ai = LocalAI(rules)
    models = _models(ai.url) if ai.local else None
    running = models is not None
    installed = running or ollama_app() is not None or shutil.which("ollama") is not None
    ready = bool(ai.enabled and ai.local and running and has_model(models or [], ai.model))
    return {"enabled": ai.enabled, "local": ai.local, "installed": installed, "running": running,
            "model": ai.model, "has_model": has_model(models or [], ai.model), "ready": ready,
            "site": OLLAMA_SITE}


def start_ollama() -> bool:
    app = ollama_app()
    if app is None:
        return False
    subprocess.Popen([str(app)], cwd=str(app.parent))  # сервер поднимется через несколько секунд
    return True


def pull_model(rules: Rules, progress: Callable[[str, float | None], None]) -> None:
    """Скачивает модель через Ollama (POST /api/pull) и сообщает ход: (что делает, доля 0..1 или None)."""
    ai = LocalAI(rules)
    if not ai.local:
        raise RuntimeError(tr("Адрес модели не на этом компьютере — скачивать не буду."))
    body = json.dumps({"model": ai.model, "stream": True}).encode("utf-8")
    request = urllib.request.Request(ai.url + "/api/pull", data=body, headers={"Content-Type": "application/json"})
    try:
        with _OPENER.open(request, timeout=120) as resp:
            for raw in resp:  # по строке JSON на каждый шаг
                if not raw.strip():
                    continue
                step = json.loads(raw)
                if step.get("error"):
                    raise RuntimeError(f"Ollama: {step['error']}")
                total, done = step.get("total"), step.get("completed")
                progress(str(step.get("status", "")), done / total if total and done is not None else None)
    except urllib.error.URLError as exc:
        raise RuntimeError(tr("Ollama не отвечает — запусти её и попробуй снова.")) from exc
