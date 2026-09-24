"""Локальная ИИ-модель через Ollama: помогает разложить по секторам то, что не поймали правила.

Всё работает на твоём компьютере — имена и начало текста документов в интернет не уходят.
Включается в правилах: [ai] enabled = true (нужен установленный Ollama и скачанная модель).
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

from . import config
from .fsutil import long_path
from .rules import Rules

TEXT_EXTS = {"txt", "md", "csv", "tsv", "json", "xml", "html", "htm", "ini", "cfg", "log", "py", "sql"}
OFFICE_PARTS = {
    "docx": ["word/document.xml"],
    "pptx": [f"ppt/slides/slide{i}.xml" for i in range(1, 6)],
    "xlsx": ["xl/sharedStrings.xml"],
}
SNIPPET = 1500


def text_snippet(path: Path) -> str:
    """Начало текста документа (txt, docx, pptx, xlsx) — чтобы модель поняла, о чём он."""
    ext = path.suffix.lower().lstrip(".")
    try:
        if ext in TEXT_EXTS:
            with open(long_path(path), "rb") as fh:
                return fh.read(SNIPPET * 2).decode("utf-8", "ignore")[:SNIPPET]
        if ext in OFFICE_PARTS:
            chunks = []
            with zipfile.ZipFile(long_path(path)) as archive:
                for part in OFFICE_PARTS[ext]:
                    if part in archive.namelist():
                        xml = archive.read(part)[:200_000].decode("utf-8", "ignore")
                        chunks.append(re.sub(r"<[^>]+>", " ", xml))
            return re.sub(r"\s+", " ", " ".join(chunks)).strip()[:SNIPPET]
    except (OSError, zipfile.BadZipFile, KeyError, ValueError):
        return ""
    return ""


class LocalAI:
    def __init__(self, rules: Rules) -> None:
        self.enabled = bool(rules.get("ai.enabled", False))
        self.url = str(rules.get("ai.url", "http://localhost:11434")).rstrip("/")
        self.model = str(rules.get("ai.model", "qwen2.5:7b"))
        self._available: bool | None = None
        self._cache_path = config.DATA_DIR / "ai_cache.json"
        try:
            self._cache: dict[str, dict] = json.loads(self._cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self._cache = {}

    def available(self) -> bool:
        if not self.enabled:
            return False
        if self._available is None:
            try:
                with urllib.request.urlopen(self.url + "/api/tags", timeout=3) as resp:
                    models = [m.get("name", "") for m in json.loads(resp.read()).get("models", [])]
                self._available = any(m == self.model or m.split(":")[0] == self.model for m in models)
            except (OSError, ValueError, urllib.error.URLError):
                self._available = False
        return self._available

    def _ask(self, prompt: str) -> dict:
        body = json.dumps({
            "model": self.model, "prompt": prompt, "stream": False, "format": "json",
            "options": {"temperature": 0},
        }).encode("utf-8")
        request = urllib.request.Request(self.url + "/api/generate", data=body,
                                         headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=180) as resp:
                data = json.loads(resp.read())
            answer = json.loads(data.get("response") or "{}")
            return answer if isinstance(answer, dict) else {}
        except (OSError, ValueError, urllib.error.URLError):
            return {}

    def save(self) -> None:
        if self._cache:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            self._cache_path.write_text(json.dumps(self._cache, ensure_ascii=False), encoding="utf-8")

    def classify(self, name: str, sectors: list[dict], sources: list[str], snippet: str,
                 cache_key: str) -> tuple[str | None, str]:
        """Сектор для файла или папки — или None, если модель не уверена."""
        names = [s["name"] for s in sectors]
        if not names:
            return None, ""
        cached = self._cache.get(cache_key)
        if cached is None:
            described = "\n".join(
                f"- {s['name']}: " + ", ".join(s.get("keywords", [])[:12] + s.get("sources", [])[:5])
                for s in sectors
            )
            prompt = (
                "You sort a person's files into folders by topic. Folders:\n"
                f"{described}\n\n"
                f"File name: {name}\n"
                f"Downloaded from: {', '.join(sources) or 'unknown'}\n"
                f"Beginning of the content: {snippet or '(not available)'}\n\n"
                "Answer strictly as JSON: {\"folder\": \"<exact folder name from the list, or empty "
                "if none fits or you are not sure>\", \"why\": \"<short reason in Russian>\"}"
            )
            cached = self._ask(prompt)
            self._cache[cache_key] = cached
        folder = str(cached.get("folder", "")).strip()
        return (folder, str(cached.get("why", "")).strip()) if folder in names else (None, "")
