"""Окно программы: маленький сервер на этом компьютере и окно Edge без адресной строки.

В сеть ничего не уходит: сервер слушает только 127.0.0.1, а каждый запрос к API несёт секретный
токен из адреса окна — чужая страница в браузере не сможет ни удалить, ни прочитать твои файлы.
"""
from __future__ import annotations

import json
import os
import secrets
import subprocess
import threading
import time
import traceback
import webbrowser
from collections.abc import Callable
from contextlib import contextmanager
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .. import __version__, ai_setup, config, i18n, journal, licensing, night, review, schedule, settings, update
from ..ai import LocalAI
from ..analyzers import Finding
from ..fsutil import display, is_under
from ..i18n import retranslate, tr
from ..rules import Rules, ensure_user_rules
from ..winutil import Busy, exclusive, system_exe

STATIC = Path(__file__).with_name("static")
FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
    "/i18n.js": ("i18n.js", "text/javascript; charset=utf-8"),
}
CSP = "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'none'"
WINDOW_GONE = 20     # с без запросов после закрытия окна — программа завершается
IDLE_EXIT = 600      # с без запросов вообще (окно свёрнуто надолго или браузер без окна Edge)
AI_TTL = 30          # с, сколько помнить, отвечает ли Ollama
LOG_LINES = 400


class ApiError(Exception):
    def __init__(self, status: HTTPStatus, message: str) -> None:
        super().__init__(message)
        self.status = status


# ======================================================================= фоновая задача
class Task:
    """Долгая операция (просмотр, ночь): ход работы и журнал видны в окне, пока она идёт."""

    def __init__(self, kind: str, title: str) -> None:
        self.kind, self.title = kind, title
        self.started = time.time()
        self.finished: float | None = None
        self.progress = ""
        self.fraction: float | None = None  # 0..1, если известно, сколько осталось (скачивание модели)
        self.countdown: int | None = None   # секунды до сна/выключения — окно показывает «Не усыплять»
        self.log: list[str] = []
        self.result: dict | None = None
        self.error: str | None = None
        self.stop = threading.Event()  # «не усыплять»: работу не прерывает, только сон/выключение в конце

    @property
    def running(self) -> bool:
        return self.finished is None

    def say(self, text: str) -> None:
        self.log.append(text)
        del self.log[:-LOG_LINES]

    def snapshot(self) -> dict:
        return {
            "kind": self.kind, "title": self.title, "running": self.running,
            "started": self.started, "finished": self.finished, "progress": self.progress, "fraction": self.fraction,
            "countdown": self.countdown,
            "log": list(self.log), "result": self.result, "error": self.error,
        }


# ======================================================================= что показывает окно
def _items(findings: list[Finding], limit: int = 200) -> list[dict]:
    return [{"path": display(f.path), "size": f.size, "reason": f.reason,
             "keep": display(f.original) if f.original else ""}
            for f in sorted(findings, key=lambda f: -f.size)[:limit]]


def _group(findings: list[Finding], with_items: bool = True) -> dict:
    data = {"count": len(findings), "bytes": sum(f.size for f in findings)}
    if with_items:
        data["items"] = _items(findings)
    return data


def plan_summary(plan: night.NightPlan) -> dict:
    waiting = plan.morning + [f for f, _ in plan.held]
    return {
        "junk": _group(plan.check.by_mode("delete"), with_items=False),
        "auto": _group(plan.auto),
        "waiting": _group(waiting),
        "report": _group(plan.check.by_mode("report"), with_items=False),
        "sort": [{"path": display(p), "subfolders": sub} for p, sub in plan.sort_folders],
        "notes": list(dict.fromkeys(plan.check.notes)),
    }


def night_summary(out: night.NightResult) -> dict:
    return {
        "freed": out.junk_freed + out.auto_freed, "junk_freed": out.junk_freed, "junk_count": out.junk_deleted,
        "auto_freed": out.auto_freed, "auto_count": len(out.auto_deleted),
        "sorted": sum(r.moved for _, _, r in out.sorted),
        "waiting": out.waiting, "waiting_bytes": out.waiting_bytes,
        "notes": list(dict.fromkeys(out.notes)), "errors": out.errors[:50], "errors_count": len(out.errors),
        "report": str(out.report or ""),
    }


def review_list() -> list[dict]:
    batches = []
    for batch in review.find_batches():
        items = [{
            "id": e["staged"], "name": Path(e["staged"]).name, "group": retranslate(e.get("group", "")),
            "reason": retranslate(e.get("reason", "")), "size": e.get("size", 0), "dir": bool(e.get("dir")),
            "from": display(Path(e["original"]).parent), "keep": display(e["keep"]) if e.get("keep") else "",
        } for e in batch.present()]
        if items:
            batches.append({"batch": str(batch.path), "name": batch.name,
                            "created": batch.created.isoformat(timespec="minutes"), "items": items})
    return batches


def _still_undoable(session: journal.SessionInfo) -> int:
    """Сколько действий отмена действительно вернёт: перенесённое, которое уже удалили, не считается,
    пустые папки — тоже (кнопка «Отменить» ради пустых папок только сбивала бы с толку)."""
    if not session.undoable:
        return 0
    alive = sum(1 for e in session.ops("move", "stage") if e.get("dst") and os.path.lexists(e["dst"]))
    return alive + len(session.ops("link", "reg_set", "lnk_set", "compress"))


def history_list(limit: int = 60) -> list[dict]:
    return [{
        "id": s.id, "kind": s.kind, "title": retranslate(s.title), "created": s.created.isoformat(timespec="minutes"),
        "summary": s.summary(), "undoable": _still_undoable(s), "undone": s.undone,
        "deleted": len(s.ops("delete")),
    } for s in journal.list_sessions()[:limit]]


# ======================================================================= приложение
class App:
    def __init__(self, load_rules: Callable[[], Rules]) -> None:
        self.load_rules = load_rules
        self.token = secrets.token_urlsafe(24)
        self.task: Task | None = None
        self.lock = threading.Lock()
        self.last_seen = time.monotonic()
        self._ai: tuple[float, dict] | None = None
        self.update: dict | None = None  # вышедшая новая версия, если есть
        self._license: tuple[float, dict] | None = None
        self.apply_language()

    def apply_language(self) -> None:
        i18n.set_language(i18n.resolve(self.load_rules().get("ui.language", "auto")))

    @property
    def busy(self) -> bool:
        return self.task is not None and self.task.running

    # ---------------------------------------------------------------- состояние
    def ai_status(self) -> dict:
        if self._ai is None or time.monotonic() - self._ai[0] > AI_TTL:
            ai = LocalAI(self.load_rules())
            self._ai = (time.monotonic(), {"enabled": ai.enabled, "local": ai.local,
                                           "available": ai.available(), "model": ai.model})
        return self._ai[1]

    def status(self) -> dict:
        waiting = [item for b in review_list() for item in b["items"]]
        return {
            "version": __version__, "lang": i18n.language(), "ai": self.ai_status(), "last": night.last_run(),
            "review": {"count": len(waiting), "bytes": sum(i["size"] for i in waiting)},
            "task": self.task.snapshot() if self.task else None, "update": self.update,
            "license": self.license_status(),
        }

    def check_update(self) -> dict:
        """Ты нажал «Проверить сейчас»: спрашиваем GitHub сразу."""
        try:
            self.update = update.check(self.load_rules(), force=True)
        except update.Unreachable as exc:
            raise ApiError(HTTPStatus.BAD_GATEWAY, tr("GitHub не ответил — проверь интернет и попробуй ещё раз.")) from exc
        return {"update": self.update, "version": __version__}

    def check_update_later(self) -> None:
        """Раз в день, в фоне и молча: вышла ли новая версия (если проверка не выключена)."""
        def work() -> None:
            try:
                self.update = update.check(self.load_rules())
            except Exception:  # noqa: BLE001 — проверка обновлений не должна мешать работе
                pass
        threading.Thread(target=work, name="filecleaner-update", daemon=True).start()

    def license_status(self) -> dict:
        if self._license is None or time.monotonic() - self._license[0] > AI_TTL:
            self._license = (time.monotonic(), licensing.status())
        return self._license[1]

    def require_license(self) -> None:
        """Чистить, раскладывать и удалять — в пробный период или с ключом."""
        self._license = None  # пробный период мог закончиться только что
        try:
            licensing.require()
        except licensing.LicenseError as exc:
            raise ApiError(HTTPStatus.PAYMENT_REQUIRED, str(exc)) from exc

    def activate_license(self, key: str) -> dict:
        try:
            info = licensing.activate(key)
        except licensing.LicenseError as exc:
            raise ApiError(HTTPStatus.BAD_REQUEST, str(exc)) from exc
        self._license = None
        return info

    # ---------------------------------------------------------------- задачи
    def start(self, kind: str, title: str, work: Callable[[Task], dict]) -> dict:
        with self.lock:
            if self.busy:
                raise ApiError(HTTPStatus.CONFLICT, tr("Уже идёт другая операция — дождись её конца."))
            task = self.task = Task(kind, title)

        def runner() -> None:
            try:
                task.result = work(task)
            except Exception as exc:  # noqa: BLE001 — ошибку показываем в окне, а не теряем в потоке
                task.error = str(exc) or exc.__class__.__name__
                task.say(tr("Ошибка: {error}", error=task.error))
                task.say(traceback.format_exc(limit=5))
            finally:
                task.finished = time.time()
                self._ai = None

        threading.Thread(target=runner, name=f"filecleaner-{kind}", daemon=True).start()
        return task.snapshot()

    def preview(self) -> dict:
        def work(task: Task) -> dict:
            task.say(tr("Ищу мусор и копии на всех дисках — ничего не трогаю…"))
            plan = night.plan_night(self.load_rules(), lambda text: setattr(task, "progress", text))
            return plan_summary(plan)
        return self.start("preview", tr("Смотрю, что можно сделать"), work)

    def run_night(self, after: str) -> dict:
        if after not in night.AFTER:
            raise ApiError(HTTPStatus.BAD_REQUEST, tr("Непонятно, что сделать в конце."))
        self.require_license()

        def work(task: Task) -> dict:
            log_path = config.DATA_DIR / "logs" / f"night-{time.strftime('%Y%m%d-%H%M')}.log"
            log_path.parent.mkdir(parents=True, exist_ok=True)

            def log(text: str) -> None:
                task.say(text)
                with open(log_path, "a", encoding="utf-8") as fh:
                    fh.write(text + "\n")

            out = night.run_night(self.load_rules(), log, lambda text: setattr(task, "progress", text))
            result = night_summary(out)
            task.result = result  # итог виден в окне и во время обратного отсчёта перед сном
            if after in ("sleep", "shutdown"):
                for left in range(60, 0, -1):
                    if task.stop.is_set():
                        log(tr("Отменено — компьютер остаётся включённым."))
                        break
                    task.countdown = left
                    task.progress = tr("Через {left} с усыплю компьютер", left=left) if after == "sleep" \
                        else tr("Через {left} с выключу компьютер", left=left)
                    time.sleep(1)
                else:
                    night.after(after, log, delay=0)
            task.progress, task.countdown = "", None
            return result
        return self.start("night", tr("Приступаю: чистка и сортировка"), work)

    # ---------------------------------------------------------------- решения
    @contextmanager
    def files_lock(self):
        """Файлы трогает одна операция за раз — и среди окон, и вместе с ночным запуском из Планировщика."""
        try:
            with exclusive():
                yield
        except Busy as exc:
            raise ApiError(HTTPStatus.CONFLICT, str(exc)) from exc

    def resolve(self, action: str, items: list[dict]) -> dict:
        if action not in ("delete", "restore"):
            raise ApiError(HTTPStatus.BAD_REQUEST, tr("Можно только удалить или вернуть."))
        if action == "delete":
            self.require_license()  # вернуть на место можно всегда
        if self.busy:
            raise ApiError(HTTPStatus.CONFLICT, tr("Идёт другая операция — дождись её конца."))
        wanted: dict[str, set[str]] = {}
        for item in items:
            wanted.setdefault(str(item.get("batch", "")), set()).add(str(item.get("id", "")))
        batches = {str(b.path): b for b in review.find_batches()}  # только настоящие партии, не любые пути
        total = review.ApproveResult()
        title = tr("Удалено из «Ready for approval»") if action == "delete" else tr("Возвращено из «Ready for approval»")
        with self.files_lock(), journal.Session("approve", title) as session:
            for batch_path, ids in wanted.items():
                batch = batches.get(batch_path)
                if batch is None:
                    total.errors.append(tr("Партия не найдена: {path}", path=batch_path))
                    continue
                ids &= {e["staged"] for e in batch.entries}
                restore, delete = (ids, set()) if action == "restore" else (set(), ids)
                result = review.resolve(batch, session, restore, delete)
                total.deleted += result.deleted
                total.freed += result.freed
                total.restored += result.restored
                total.errors += result.errors
        return {"deleted": total.deleted, "freed": total.freed, "restored": len(total.restored),
                "errors": total.errors}

    def undo(self, session_id: str) -> dict:
        if self.busy:
            raise ApiError(HTTPStatus.CONFLICT, tr("Идёт другая операция — дождись её конца."))
        info = journal.find_session(session_id)
        if info is None:
            raise ApiError(HTTPStatus.NOT_FOUND, tr("Такой операции нет в истории."))
        if not info.undoable:
            raise ApiError(HTTPStatus.BAD_REQUEST, tr("Эту операцию отменить нельзя."))
        with self.files_lock():
            result = journal.undo(info)
        return {"restored": result.restored, "irreversible": result.irreversible,
                "missing": result.missing[:20], "errors": result.errors[:20]}

    def open_path(self, what: str, path: str) -> dict:
        target = Path(path)
        if what == "report":
            ours = is_under(target, config.DATA_DIR / "reports") or (
                target.name == review.REPORT and any(is_under(target, root) for root in review.review_roots()))
            if not ours or target.suffix.lower() != ".html" or not target.exists():
                raise ApiError(HTTPStatus.BAD_REQUEST, tr("Это не отчёт программы."))
            os.startfile(target)  # type: ignore[attr-defined]  # откроется в браузере
        elif what == "buy":
            if not licensing.BUY_URL:
                raise ApiError(HTTPStatus.NOT_FOUND, tr("Ссылки на магазин пока нет."))
            webbrowser.open(licensing.BUY_URL)
        elif what == "update":
            webbrowser.open((self.update or {}).get("url") or update.PAGE + "latest")  # только страница выпусков
        elif what == "ollama-site":
            webbrowser.open(ai_setup.OLLAMA_SITE)  # официальный сайт; сам установщик не скачиваем
        elif what == "rules":
            subprocess.Popen([system_exe("notepad.exe"), str(ensure_user_rules())])  # для тех, кто хочет править руками
        elif what == "reveal":
            if not os.path.lexists(target):
                raise ApiError(HTTPStatus.NOT_FOUND, tr("Файла уже нет на месте."))
            subprocess.Popen([str(config.WINDIR / "explorer.exe"), f"/select,{target}"])  # только показывает
        else:
            raise ApiError(HTTPStatus.BAD_REQUEST, tr("Непонятно, что открыть."))
        return {"ok": True}

    def pull_model(self) -> dict:
        def work(task: Task) -> dict:
            model = LocalAI(self.load_rules()).model
            task.say(tr("Скачиваю модель {model} через Ollama — это несколько гигабайт, можно заниматься своим.", model=model))
            last = [""]

            def progress(step: str, fraction: float | None) -> None:
                if fraction is not None:  # шаги без размеров (проверка, запись) шкалу не сбрасывают
                    task.fraction = fraction
                task.progress = f"{step} — {fraction:.0%}" if fraction is not None else step
                if step != last[0]:
                    last[0] = step
                    task.say(step)

            ai_setup.pull_model(self.load_rules(), progress)
            task.fraction = 1.0
            task.say(tr("Готово: модель на месте."))
            return {"model": model}
        return self.start("pull", tr("Скачиваю модель для ИИ"), work)

    def disable_ai(self) -> dict:
        data = settings.read(self.load_rules())
        data["ai"]["enabled"] = False
        return self.save_settings(data)

    def read_settings(self) -> dict:
        return {**settings.read(self.load_rules()), "schedule": schedule.status()}

    def save_settings(self, body: dict) -> dict:
        try:
            settings.save(body)
            if isinstance(body.get("schedule"), dict):  # задача в Планировщике — отдельно от файла правил
                schedule.apply(body["schedule"])
        except (settings.SettingsError, schedule.ScheduleError) as exc:
            raise ApiError(HTTPStatus.BAD_REQUEST, str(exc)) from exc
        self._ai = None  # модель или «включён» могли поменяться
        self.apply_language()
        if not (body.get("update") or {}).get("check", True):
            self.update = None  # проверку выключили — и напоминание убираем
        return self.read_settings()

    def reveal_item(self, batch_path: str, item_id: str) -> dict:
        batch = next((b for b in review.find_batches() if str(b.path) == batch_path), None)
        if batch is None or item_id not in {e["staged"] for e in batch.entries}:
            raise ApiError(HTTPStatus.NOT_FOUND, tr("Файла уже нет в «Ready for approval»."))
        return self.open_path("reveal", str(batch.path / item_id))

    # ---------------------------------------------------------------- маршруты
    def get(self, path: str) -> dict | list:
        routes = {"/api/status": self.status, "/api/review": review_list, "/api/history": history_list,
                  "/api/task": lambda: self.task.snapshot() if self.task else None,
                  "/api/settings": self.read_settings,
                  "/api/ai-setup": lambda: ai_setup.status(self.load_rules())}
        if path not in routes:
            raise ApiError(HTTPStatus.NOT_FOUND, tr("Нет такого раздела."))
        return routes[path]()

    def post(self, path: str, body: dict) -> dict:
        if path == "/api/preview":
            return self.preview()
        if path == "/api/night":
            return self.run_night(str(body.get("after", "nothing")))
        if path == "/api/resolve":
            return self.resolve(str(body.get("action", "")), list(body.get("items") or []))
        if path == "/api/undo":
            return self.undo(str(body.get("id", "")))
        if path == "/api/open":
            return self.open_path(str(body.get("what", "")), str(body.get("path", "")))
        if path == "/api/reveal":
            return self.reveal_item(str(body.get("batch", "")), str(body.get("id", "")))
        if path == "/api/settings":
            return self.save_settings(body)
        if path == "/api/ai/start":
            started = ai_setup.start_ollama()
            self._ai = None
            return {"started": started}
        if path == "/api/ai/pull":
            return self.pull_model()
        if path == "/api/ai/disable":
            return self.disable_ai()
        if path == "/api/license":
            return self.activate_license(str(body.get("key", "")))
        if path == "/api/update":
            return self.check_update()
        if path == "/api/stay-awake":
            if self.task is not None:
                self.task.stop.set()
            return {"ok": True}
        raise ApiError(HTTPStatus.NOT_FOUND, tr("Нет такого действия."))


# ======================================================================= HTTP
class Handler(BaseHTTPRequestHandler):
    server: "Server"
    protocol_version = "HTTP/1.1"

    def log_message(self, *args) -> None:  # тихо: окну консоль не нужна
        pass

    def _send(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", CSP)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: HTTPStatus, data) -> None:
        self._send(status, json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _allowed(self) -> bool:
        """Только это окно: свой адрес (защита от подмены DNS) и секретный токен."""
        port = self.server.server_address[1]
        host_ok = self.headers.get("Host", "") in (f"127.0.0.1:{port}", f"localhost:{port}")
        token_ok = secrets.compare_digest(self.headers.get("X-Token", ""), self.server.app.token)
        return host_ok and token_ok

    def _api(self, call: Callable[[], object]) -> None:
        if not self._allowed():
            self._json(HTTPStatus.FORBIDDEN, {"error": tr("Нет доступа.")})
            return
        self.server.app.last_seen = time.monotonic()
        try:
            self._json(HTTPStatus.OK, call())
        except ApiError as exc:
            self._json(exc.status, {"error": str(exc)})
        except Exception as exc:  # noqa: BLE001
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc) or exc.__class__.__name__})

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0].split("#", 1)[0]
        if path.startswith("/api/"):
            self._api(lambda: self.server.app.get(path))
            return
        if path not in FILES:
            self._send(HTTPStatus.NOT_FOUND, b"", "text/plain")
            return
        name, content_type = FILES[path]
        body = (STATIC / name).read_bytes()
        if name == "index.html":  # язык окна — сразу в разметке, чтобы не мелькал русский текст
            body = body.replace(b'<html lang="ru">', f'<html lang="{i18n.language()}">'.encode())
        self._send(HTTPStatus.OK, body, content_type)

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        length = int(self.headers.get("Content-Length") or 0)
        if length > 1_000_000:
            self._json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": tr("Слишком большой запрос.")})
            return
        raw = self.rfile.read(length) if length else b"{}"

        def call() -> object:
            try:
                body = json.loads(raw or b"{}")
            except ValueError as exc:
                raise ApiError(HTTPStatus.BAD_REQUEST, tr("Запрос не в JSON.")) from exc
            return self.server.app.post(path, body if isinstance(body, dict) else {})

        self._api(call)


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, app: App, port: int = 0) -> None:
        super().__init__(("127.0.0.1", port), Handler)
        self.app = app

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}/#token={self.app.token}"


# ======================================================================= окно
def find_edge() -> Path | None:
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"), os.environ.get("LOCALAPPDATA")):
        if base:
            candidate = Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe"
            if candidate.exists():
                return candidate
    return None


def open_window(url: str) -> subprocess.Popen | None:
    """Окно Edge без адресной строки (своя папка профиля — не мешает твоему браузеру); иначе — браузер."""
    edge = find_edge()
    if edge is None:
        webbrowser.open(url)
        return None
    profile = config.DATA_DIR / "window"
    return subprocess.Popen([str(edge), f"--app={url}", f"--user-data-dir={profile}", "--window-size=1240,860",
                             "--no-first-run", "--no-default-browser-check"])


def run(load_rules: Callable[[], Rules] = Rules.load, port: int = 0, show_window: bool = True) -> int:
    app = App(load_rules)
    server = Server(app, port)
    threading.Thread(target=server.serve_forever, name="filecleaner-http", daemon=True).start()
    app.check_update_later()
    window = open_window(server.url) if show_window else None
    if not show_window:
        print(tr("Окно программы: {url}\nЗакрыть — Ctrl+C.", url=server.url), flush=True)
    try:
        while True:
            time.sleep(1)
            if app.busy:
                continue  # операция идёт — даже если окно закрыли, доделываем
            idle = time.monotonic() - app.last_seen
            window_closed = window is not None and window.poll() is not None
            if (window_closed and idle > WINDOW_GONE) or (show_window and idle > IDLE_EXIT):
                break
    except KeyboardInterrupt:
        pass
    server.shutdown()
    server.server_close()
    return 0
