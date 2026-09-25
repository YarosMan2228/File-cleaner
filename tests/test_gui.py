"""Окно программы: решения без перетаскивания, доступ только по токену, долгие операции в фоне."""
import json
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from conftest import write

from filecleaner import config, journal, review
from filecleaner.analyzers import Finding
from filecleaner.gui.app import App, Server, Task

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
