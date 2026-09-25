"""HTML-отчёты: открываются в браузере, есть поиск по строкам, светлая и тёмная тема."""
from __future__ import annotations

import html
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import config
from .i18n import language, tr

MAX_ROWS = 5000


@dataclass
class Section:
    title: str
    columns: list[str]
    rows: list[list[str]] = field(default_factory=list)
    note: str = ""
    open: bool = False


_CSS = """
:root { --bg:#f7f7f5; --card:#fff; --text:#1d1d1f; --muted:#6b6b70; --line:#e4e4e0; --accent:#2f6fde; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#161618; --card:#202023; --text:#ececec; --muted:#9a9aa0; --line:#34343a; --accent:#7aa7ff; }
}
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--text); font:15px/1.5 "Segoe UI", system-ui, sans-serif; }
main { max-width:1200px; margin:0 auto; padding:24px 16px 64px; }
h1 { font-size:24px; margin:0 0 4px; }
.sub { color:var(--muted); margin:0 0 20px; }
.cards { display:flex; flex-wrap:wrap; gap:12px; margin-bottom:20px; }
.card { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:10px 16px; min-width:140px; }
.card b { display:block; font-size:20px; }
.card span { color:var(--muted); font-size:13px; }
input { width:100%; padding:10px 12px; border-radius:8px; border:1px solid var(--line); background:var(--card);
        color:var(--text); font:inherit; margin-bottom:16px; }
details { background:var(--card); border:1px solid var(--line); border-radius:10px; margin-bottom:12px; }
summary { cursor:pointer; padding:12px 16px; font-weight:600; }
summary small { color:var(--muted); font-weight:400; margin-left:8px; }
.wrap { overflow-x:auto; }
table { width:100%; border-collapse:collapse; font-size:13px; }
th, td { text-align:left; padding:6px 16px; border-top:1px solid var(--line); vertical-align:top; }
th { color:var(--muted); font-weight:600; }
td { overflow-wrap:anywhere; }
.more { color:var(--muted); padding:8px 16px; }
"""

_JS = """
const q = document.getElementById('q');
q.addEventListener('input', () => {
  const t = q.value.toLowerCase();
  document.querySelectorAll('tbody tr').forEach(r => {
    r.style.display = r.textContent.toLowerCase().includes(t) ? '' : 'none';
  });
  if (t) document.querySelectorAll('details').forEach(d => d.open = true);
});
"""


def render(title: str, subtitle: str, cards: list[tuple[str, str]], sections: list[Section]) -> str:
    esc = html.escape
    parts = [
        f"<!doctype html><html lang='{language()}'><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width, initial-scale=1'>",
        f"<title>{esc(title)}</title><style>{_CSS}</style></head><body><main>",
        f"<h1>{esc(title)}</h1><p class='sub'>{esc(subtitle)}</p>",
        "<div class='cards'>",
        *(f"<div class='card'><b>{esc(value)}</b><span>{esc(label)}</span></div>" for label, value in cards),
        f"</div><input id='q' placeholder='{esc(tr('Поиск по имени, папке или причине…'))}'>",
    ]
    for section in sections:
        parts.append(f"<details{' open' if section.open else ''}><summary>{esc(section.title)}"
                     f"<small>{esc(section.note)}</small></summary><div class='wrap'><table><thead><tr>")
        parts += [f"<th>{esc(c)}</th>" for c in section.columns]
        parts.append("</tr></thead><tbody>")
        for row in section.rows[:MAX_ROWS]:
            parts.append("<tr>" + "".join(f"<td>{esc(str(cell))}</td>" for cell in row) + "</tr>")
        parts.append("</tbody></table></div>")
        if len(section.rows) > MAX_ROWS:
            parts.append(f"<div class='more'>{esc(tr('…и ещё {count} строк', count=len(section.rows) - MAX_ROWS))}</div>")
        parts.append("</details>")
    parts.append(f"<script>{_JS}</script></main></body></html>")
    return "".join(parts)


def save(name: str, content: str) -> Path:
    folder = config.DATA_DIR / "reports"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{datetime.now():%Y%m%d-%H%M%S}-{name}.html"
    path.write_text(content, encoding="utf-8")
    return path
