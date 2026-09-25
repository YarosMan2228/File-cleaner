"""Окно программы: решения без перетаскивания, доступ только по токену, долгие операции в фоне."""
import json
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
from conftest import write

from filecleaner import ai_setup, config, journal, review
from filecleaner.analyzers import Finding
from filecleaner.gui.app import App, Server, Task
from filecleaner.rules import Rules

NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def stage(sandbox: Path) -> tuple[review.Batch, dict[str, Path]]:
    files = {name: write(sandbox / "Downloads" / name, name.encode() * 50) for name in ("a (1).pdf", "b (1).pdf", "c (1).pdf")}
    findings = [Finding("duplicates.copy_names", "Дубликаты - имя-копия", path, path.stat().st_size, "review", "копия")
                for path in files.values()]
    with journal.Session("check", "Проверка") as session:
        [batch], errors = review.stage(findings, session)
    assert not errors and not any(p.exists() for p in files.values())
    return batch, files


def staged_ids(batch: review.Batch) -> dict[str, str]:
    return {Path(e["original"]).name: e["staged"] for e in batch.entries}


def test_resolve_restores_deletes_and_keeps_the_rest(sandbox):
    batch, files = stage(sandbox)
    ids = staged_ids(batch)
    with journal.Session("approve", "Решение") as session:
        result = review.resolve(batch, session, restore={ids["a (1).pdf"]}, delete={ids["b (1).pdf"]})

    assert files["a (1).pdf"].exists() and result.restored               # вернулся на место
    assert result.deleted == 1 and not files["b (1).pdf"].exists()       # удалён
    [left] = review.find_batches()                                       # «c» ждёт дальше
    assert [Path(e["original"]).name for e in left.present()] == ["c (1).pdf"]


# ======================================================================= сервер
@pytest.fixture
def gui(sandbox, rules, monkeypatch):
    # Только папки песочницы: без этого пути «Загрузок» и «Документов» взялись бы из реестра — настоящие.
    rules.data["night"]["drives"] = False
    folders = {name.lower(): sandbox / name for name in ("Downloads", "Documents", "Desktop")}
    monkeypatch.setattr(config, "user_folders", lambda: folders)
    server = Server(App(lambda: rules))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()
    server.server_close()


def call(server: Server, path: str, body: dict | None = None, token: str | None = None):
    request = urllib.request.Request(
        f"http://127.0.0.1:{server.server_address[1]}{path}",
        data=None if body is None else json.dumps(body).encode("utf-8"),
        headers={"X-Token": server.app.token if token is None else token, "Content-Type": "application/json"},
    )
    try:
        with NO_PROXY.open(request, timeout=30) as response:
            raw = response.read()
            return response.status, (json.loads(raw) if raw.startswith((b"{", b"[", b"n")) else raw)
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"null")


def test_api_needs_the_window_token(gui):
    assert call(gui, "/api/status", token="чужая страница".encode().hex())[0] == 403
    status, data = call(gui, "/api/status")
    assert status == 200 and {"ai", "review", "last", "task"} <= set(data)
    status, page = call(gui, "/")                                  # сама страница — без секретов
    assert status == 200 and b"File Cleaner" in page
    assert call(gui, "/../../secret.txt")[0] == 404


def test_review_decisions_by_buttons(gui, sandbox):
    batch, files = stage(sandbox)
    ids = staged_ids(batch)
    status, batches = call(gui, "/api/review")
    assert status == 200 and len(batches[0]["items"]) == 3

    status, result = call(gui, "/api/resolve", {"action": "delete", "items": [
        {"batch": str(batch.path), "id": ids["b (1).pdf"]},
        {"batch": str(sandbox / "Downloads"), "id": "a (1).pdf"},       # не партия — не трогаем
    ]})
    assert status == 200 and result["deleted"] == 1 and result["errors"]
    assert not (batch.path / ids["b (1).pdf"]).exists() and (batch.path / ids["a (1).pdf"]).exists()

    status, result = call(gui, "/api/resolve", {"action": "restore", "items": [
        {"batch": str(batch.path), "id": ids["a (1).pdf"]}]})
    assert status == 200 and result["restored"] == 1 and files["a (1).pdf"].exists()


def test_undo_from_history(gui, sandbox):
    stage(sandbox)
    status, sessions = call(gui, "/api/history")
    [check] = [s for s in sessions if s["kind"] == "check"]
    assert status == 200 and check["undoable"] == 3
    status, result = call(gui, "/api/undo", {"id": check["id"]})
    assert status == 200 and result["restored"] == 3
    assert (sandbox / "Downloads" / "a (1).pdf").exists()


def test_preview_runs_in_background_and_one_task_at_a_time(gui, sandbox):
    write(sandbox / "Downloads" / "report.pdf", b"r" * 5000)
    write(sandbox / "Downloads" / "report (1).pdf", b"r" * 5000)
    status, task = call(gui, "/api/preview", {})
    assert status == 200 and task["kind"] == "preview"
    for _ in range(100):
        status, task = call(gui, "/api/task")
        if not task["running"]:
            break
        time.sleep(0.1)
    assert task["error"] is None and task["result"]["auto"]["count"] == 1   # копия в Загрузках — удалить сразу

    gui.app.task = Task("night", "идёт")                                    # пока идёт одна операция —
    assert call(gui, "/api/preview", {})[0] == 409                          # вторая не начинается


def test_only_program_reports_can_be_opened(gui, sandbox):
    other = write(sandbox / "Downloads" / "page.html", b"<script>")
    status, data = call(gui, "/api/open", {"what": "report", "path": str(other)})
    assert status == 400 and "не отчёт" in data["error"]


def test_settings_through_the_window(gui):
    status, current = call(gui, "/api/settings")
    assert status == 200 and current["sectors"] and "Документы" in current["types"]
    current["ai"]["min_confidence"] = 90
    status, _ = call(gui, "/api/settings", current)
    assert status == 200 and Rules.load().get("ai.min_confidence") == 90    # записано в файл правил
    current["sectors"].append({"name": "Изображения", "description": "", "keywords": [], "sources": [], "types": []})
    status, data = call(gui, "/api/settings", current)
    assert status == 400 and "папка типа" in data["error"]


# ======================================================================= первый запуск ИИ
class FakeOllama(BaseHTTPRequestHandler):
    models: list[str] = []

    def log_message(self, *args):
        pass

    def _reply(self, body: bytes, kind: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # /api/tags
        self._reply(json.dumps({"models": [{"name": m} for m in FakeOllama.models]}).encode(), "application/json")

    def do_POST(self):  # /api/pull — по строке JSON на шаг, как настоящая Ollama
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        steps = [{"status": "pulling manifest"}, {"status": "pulling 2bada8a7", "total": 100, "completed": 40},
                 {"status": "pulling 2bada8a7", "total": 100, "completed": 100}, {"status": "success"}]
        FakeOllama.models.append(request["model"])
        self._reply(b"".join(json.dumps(s).encode() + b"\n" for s in steps), "application/x-ndjson")


@pytest.fixture
def fake_ollama():
    server = HTTPServer(("127.0.0.1", 0), FakeOllama)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    FakeOllama.models = []
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def wait_task(server: Server) -> dict:
    for _ in range(100):
        task = call(server, "/api/task")[1]
        if not task["running"]:
            return task
        time.sleep(0.05)
    raise AssertionError("задача не закончилась")


def test_first_run_downloads_the_model(gui, rules, fake_ollama):
    rules.data["ai"].update(enabled=True, url=fake_ollama, model="qwen2.5:7b")
    setup = call(gui, "/api/ai-setup")[1]
    assert setup["running"] and not setup["has_model"] and not setup["ready"]

    assert call(gui, "/api/ai/pull", {})[0] == 200
    task = wait_task(gui)
    assert task["error"] is None and task["fraction"] == 1.0 and "Готово" in task["log"][-1]
    assert call(gui, "/api/ai-setup")[1]["ready"]


def test_without_ollama_ai_can_be_switched_off(gui, rules, monkeypatch):
    monkeypatch.setattr(ai_setup, "ollama_app", lambda: None)
    monkeypatch.setattr(ai_setup.shutil, "which", lambda name: None)
    rules.data["ai"].update(enabled=True, url="http://127.0.0.1:9")
    setup = call(gui, "/api/ai-setup")[1]
    assert not setup["installed"] and not setup["running"] and not setup["ready"]
    assert call(gui, "/api/ai/start", {})[1] == {"started": False}
    assert call(gui, "/api/ai/disable", {})[0] == 200 and Rules.load().get("ai.enabled") is False
