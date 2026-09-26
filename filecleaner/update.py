"""Вышла ли новая версия: раз в день спросить у GitHub номер последнего выпуска.

В запросе нет ничего о тебе и твоих файлах — только адрес страницы и версия программы в User-Agent
(как у любого запроса, GitHub видит IP-адрес). Скачивать и ставить программа сама ничего не будет:
покажет, что вышла новая версия, и откроет страницу выпуска, если нажать. Выключается в настройках.
"""
from __future__ import annotations

import http.client
import json
import re
import time
import urllib.error
import urllib.request

from . import __version__, config

REPO = "YarosMan2228/File-cleaner"
API_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
PAGE = f"https://github.com/{REPO}/releases/"   # открываем только страницы выпусков этого проекта
EVERY = 24 * 3600                                # с между проверками
_OPENER = urllib.request.build_opener()          # с системным прокси: это интернет, а не localhost


class Unreachable(RuntimeError):
    """GitHub не ответил: нет интернета, блокировка, сбой."""


def parse_version(text: str) -> tuple[int, ...] | None:
    found = re.fullmatch(r"[vV]?(\d{1,6}(?:\.\d{1,6}){0,3})", str(text).strip())
    return tuple(int(part) for part in found.group(1).split(".")) if found else None


def is_newer(latest: str, current: str = __version__) -> bool:
    a, b = parse_version(latest), parse_version(current)
    if a is None or b is None:
        return False
    size = max(len(a), len(b))
    return a + (0,) * (size - len(a)) > b + (0,) * (size - len(b))


def fetch(timeout: float = 10) -> dict | None:
    """Последний выпуск: {"version", "url", "notes"}; None — выпусков ещё нет."""
    request = urllib.request.Request(API_URL, headers={
        "Accept": "application/vnd.github+json", "User-Agent": f"FileCleaner/{__version__}"})
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            data = json.loads(response.read(1_000_000).decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise Unreachable(f"HTTP {exc.code}") from exc
    except (OSError, ValueError, http.client.HTTPException) as exc:
        raise Unreachable(str(exc) or exc.__class__.__name__) from exc
    return _release(data)


def _release(data: object) -> dict | None:
    if not isinstance(data, dict):
        return None
    tag = str(data.get("tag_name") or "")
    if parse_version(tag) is None:
        return None
    url = PAGE + "tag/" + tag  # адрес собираем сами: открываем только страницу выпуска этого проекта
    if str(data.get("html_url") or "") != url:
        url = PAGE + "latest"
    return {"version": tag.lstrip("vV"), "tag": tag, "url": url, "notes": str(data.get("body") or "")[:2000]}


def _state_path():
    return config.DATA_DIR / "update.json"


def _load_state() -> dict:
    try:
        state = json.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return state if isinstance(state, dict) else {}


def _save_state(state: dict) -> None:
    path = _state_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass  # не записали — проверим ещё раз при следующем запуске


def known() -> dict | None:
    """Новая версия по последней проверке — без запроса в сеть."""
    saved = _load_state().get("latest")
    if not isinstance(saved, dict):
        return None
    latest = _release({"tag_name": saved.get("tag") or saved.get("version"), "html_url": saved.get("url"),
                       "body": saved.get("notes")})
    return latest if latest and is_newer(latest["version"]) else None


def check(rules, force: bool = False) -> dict | None:
    """Новая версия ({"version", "url", "notes"}) или None.

    Обычно — не чаще раза в день и только если проверка не выключена; без сети — что знали раньше.
    force — ты сам нажал «Проверить»: спрашиваем сразу, а если GitHub не ответил — Unreachable.
    """
    if not force and not rules.get("update.check", True):
        return None
    state = _load_state()
    try:
        checked = float(state.get("checked") or 0)
    except (TypeError, ValueError):
        checked = 0.0
    if force or not 0 <= time.time() - checked < EVERY:  # отметка из будущего (часы сбились) — тоже пора
        try:
            latest = fetch()
        except Unreachable:
            if force:
                raise
            return known()
        _save_state({"checked": time.time(), "latest": latest})
    return known()
