"""Интерфейс: команды для терминала и меню на русском (запуск без аргументов)."""
from __future__ import annotations

import argparse
import os
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

from . import __version__, analyzers, compress, config, fsutil, journal, night, organizer, pipeline, report, review
from .ai import LocalAI
from .fsutil import display, human_size, plural
from .index import Index
from .rules import Rules, RulesError, ensure_user_rules

# ======================================================================= вывод
if os.name == "nt" and sys.stdout.isatty():
    os.system("")  # включает цвета ANSI в классической консоли Windows
_COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _COLOR else text


def bold(text: str) -> str:
    return _c("1", text)


def dim(text: str) -> str:
    return _c("2", text)


def green(text: str) -> str:
    return _c("32", text)


def yellow(text: str) -> str:
    return _c("33", text)


def red(text: str) -> str:
    return _c("31", text)


def cyan(text: str) -> str:
    return _c("36", text)


def objects(n: int) -> str:
    return plural(n, "объект", "объекта", "объектов")


def files(n: int) -> str:
    return plural(n, "файл", "файла", "файлов")


class Progress:
    """Строка статуса, которая перерисовывается на месте."""

    WIDTH = 78

    def __init__(self) -> None:
        self._last = 0.0
        self._shown = False
        self._enabled = sys.stderr.isatty()

    def __call__(self, text: str) -> None:
        if not self._enabled:
            return
        now = time.monotonic()
        if now - self._last < 0.1:
            return
        self._last = now
        sys.stderr.write("\r" + text[: self.WIDTH].ljust(self.WIDTH))
        sys.stderr.flush()
        self._shown = True

    def clear(self) -> None:
        if self._shown:
            sys.stderr.write("\r" + " " * self.WIDTH + "\r")
            sys.stderr.flush()
            self._shown = False


def ask(prompt: str) -> str | None:
    """Ответ пользователя; None — если ввод закрыт или нажат Ctrl+C."""
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None


def confirm(prompt: str) -> bool:
    answer = ask(f"{prompt} [д/н]: ")
    return (answer or "").lower() in {"д", "да", "y", "yes", "т", "так"}


def open_in_system(path: Path) -> None:
    try:
        os.startfile(path)  # type: ignore[attr-defined]
    except (AttributeError, OSError) as exc:
        print(red(f"Не получилось открыть {path}: {exc}"))


def line(title: str, size: int, count: str = "", width: int = 44) -> str:
    return f"{title[:width]:<{width}} {human_size(size):>10}  {dim(count)}"


# ======================================================================= скан
def cmd_scan(args, rules: Rules, interactive: bool = False) -> int:
    roots = rules.roots()
    print(bold("Скан: что занимает место"))
    for name, path in roots.items():
        print(dim(f"  {config.FOLDER_TITLES.get(name, name)}: {display(path)}"))
    progress = Progress()
    with Index() as index:
        stats = index.scan(roots, progress)
        recs = index.files(roots.keys())
    progress.clear()
    print(f"\nВ индексе {files(stats.files)} на {human_size(stats.bytes)} (скан {stats.seconds:.0f} с).")
    if stats.cloud:
        print(dim(f"Облачных файлов OneDrive, которых нет на диске: {stats.cloud} — их не трогаю."))

    local = [r for r in recs if not r.cloud]
    by_root: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    by_type: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    by_folder: dict[str, int] = defaultdict(int)
    for rec in local:
        by_root[rec.root][0] += 1
        by_root[rec.root][1] += rec.size
        kind = config.EXT_TO_TYPE.get(rec.ext, config.OTHER_TYPE)
        by_type[kind][0] += 1
        by_type[kind][1] += rec.size
        root = roots.get(rec.root)
        if root is not None:
            rel = Path(str(rec.path)[len(str(root)):].lstrip("\\/"))
            top = root / rel.parts[0] if len(rel.parts) > 1 else root / "(файлы в корне)"
            by_folder[str(top)] += rec.size

    print(bold("\nПо папкам:"))
    for name, (n, size) in sorted(by_root.items(), key=lambda kv: -kv[1][1]):
        print("  " + line(config.FOLDER_TITLES.get(name, name), size, files(n)))
    print(bold("\nПо типам:"))
    for kind, (n, size) in sorted(by_type.items(), key=lambda kv: -kv[1][1])[:12]:
        print("  " + line(kind, size, files(n)))
    print(bold("\nСамые большие подпапки:"))
    for folder, size in sorted(by_folder.items(), key=lambda kv: -kv[1])[:10]:
        print("  " + line(display(folder), size, width=60))
    print(bold("\nСамые большие файлы:"))
    for rec in sorted(local, key=lambda r: -r.size)[:10]:
        print("  " + line(display(rec.path), rec.size, width=60))
    print(dim("\nДальше: «filecleaner check» — найдёт мусор, дубли и лишнее."))
    return 0


# ======================================================================= проверка
MODE_TITLES = {
    "delete": "Удалю сразу — это восстанавливается само",
    "review": f"На проверку → папка «{config.REVIEW_DIR_NAME}» (удалится только после твоего «утвердить»)",
    "report": "Только в отчёте — решаешь сам, программа не трогает",
}


def _print_check(result: analyzers.CheckResult) -> list[str]:
    """Печатает сводку; возвращает названия групп, у которых есть номер (их можно пропустить)."""
    numbered: list[str] = []
    totals = analyzers.group_totals(result.findings)
    examples: dict[tuple[str, str], list[analyzers.Finding]] = defaultdict(list)
    for finding in sorted(result.findings, key=lambda f: -f.size):
        bucket = examples[(finding.group, finding.mode)]
        if len(bucket) < 3 and not finding.rule.startswith("junk."):
            bucket.append(finding)
    for mode in ("delete", "review", "report"):
        rows = [row for row in totals if row[1] == mode]
        if not rows:
            continue
        total = sum(r[3] for r in rows)
        print(bold(f"\n{MODE_TITLES[mode]}: {human_size(total)}"))
        for group, _, count, size in rows:
            if mode == "report":
                prefix = "     "
            else:
                numbered.append(group)
                prefix = f"{len(numbered):>3}. "
            print(prefix + line(group, size, objects(count)))
            if mode != "delete":
                for f in examples[(group, mode)]:
                    print(dim(f"        {display(f.path)} — {f.reason}"))
    if result.notes:
        print(bold("\nЗамечания:"))
        for note in dict.fromkeys(result.notes):
            print(yellow(f"  ! {note}"))
    return numbered


def _check_report(result: analyzers.CheckResult) -> str:
    groups: dict[str, list[analyzers.Finding]] = defaultdict(list)
    for finding in result.findings:
        groups[finding.group].append(finding)
    sections = []
    for group, items in sorted(groups.items(), key=lambda kv: -sum(f.size for f in kv[1])):
        mode = items[0].mode
        size = human_size(sum(f.size for f in items))
        title = f"{group} — {size} · {MODE_TITLES[mode].split(' —')[0].split(' →')[0].lower()}"
        if mode == "delete" and len(items) > 50:
            by_dir: dict[str, list[int]] = defaultdict(lambda: [0, 0])
            for f in items:
                entry = by_dir[str(f.path.parent)]
                entry[0] += 1
                entry[1] += f.size
            rows = [[display(d), str(n), human_size(s)] for d, (n, s) in sorted(by_dir.items(), key=lambda kv: -kv[1][1])]
            sections.append(report.Section(title, ["Папка", "Файлов", "Размер"], rows, note=objects(len(items))))
        else:
            rows = [[display(f.path), human_size(f.size), f.reason] for f in sorted(items, key=lambda f: -f.size)]
            sections.append(report.Section(title, ["Файл", "Размер", "Почему"], rows, note=objects(len(items))))
    cards = [(MODE_TITLES[m].split(" —")[0].split(" →")[0], human_size(sum(f.size for f in result.by_mode(m))))
             for m in ("delete", "review", "report") if result.by_mode(m)]
    return report.render("Проверка: мусор, дубли и лишнее", "Ничего не удалено — это только отчёт.", cards, sections)


def _parse_numbers(answer: str, limit: int) -> set[int] | None:
    numbers = set()
    for part in answer.replace(" ", "").split(","):
        if not part:
            continue
        if not part.isdigit() or not 1 <= int(part) <= limit:
            return None
        numbers.add(int(part))
    return numbers


def cmd_check(args, rules: Rules, interactive: bool = False) -> int:
    roots = rules.roots()
    progress = Progress()
    print(bold("Проверка: ищу мусор, дубли и лишнее…"))
    with Index() as index:
        index.scan(roots, progress)
        result = analyzers.run_check(index, roots, rules, progress)
    progress.clear()
    if not result.findings:
        print(green("Ничего лишнего не нашёл."))
        return 0
    numbered = _print_check(result)
    path = report.save("проверка", _check_report(result))
    print(dim(f"\nПодробный отчёт: {path}"))
    if interactive and confirm("Открыть отчёт в браузере?"):
        open_in_system(path)
    if not numbered:
        print("Действовать нечего: всё найденное — только для отчёта.")
        return 0

    skip: set[str] = set()
    if interactive:
        while True:
            answer = ask("\nВыполнить? Enter — да; номера групп через запятую — пропустить их; 0 — отмена: ")
            if answer is None or answer == "0":
                print("Отменено, ничего не тронуто.")
                return 0
            numbers = _parse_numbers(answer, len(numbered))
            if numbers is None:
                print(red(f"Не понял. Номера от 1 до {len(numbered)} через запятую."))
                continue
            skip = {numbered[n - 1] for n in numbers}
            break
    elif not args.apply:
        print(cyan("\nЭто был просмотр. Выполнить: filecleaner check --apply"))
        return 0
    elif not args.yes and not confirm("Удалить кэши и перенести остальное на проверку?"):
        print("Отменено, ничего не тронуто.")
        return 0

    with journal.Session("check", "Проверка") as session:
        out = pipeline.apply_check(result, session, skip, progress)
    progress.clear()
    if out.deleted:
        print(green(f"Удалено {objects(out.deleted)}, освобождено {human_size(out.freed)}."))
    if out.staged:
        print(green(f"На проверку перенесено {objects(out.staged)} ({human_size(out.staged_bytes)}):"))
        for batch in out.batches:
            print(f"  {batch.path}")
        print("Загляни туда: нужное перетащи в «_ВЕРНУТЬ», потом «filecleaner approve» удалит остальное.")
    for error in out.errors[:10]:
        print(yellow(f"  ! {error}"))
    if len(out.errors) > 10:
        print(yellow(f"  …и ещё {len(out.errors) - 10} ошибок (обычно — файл открыт в программе)."))
    print(dim("Передумал? «filecleaner undo» вернёт перенесённое на места."))
    return 0


# ======================================================================= утверждение
def cmd_approve(args, rules: Rules, interactive: bool = False) -> int:
    batches = review.find_batches()
    if not batches:
        print(f"В «{config.REVIEW_DIR_NAME}» ничего не ждёт утверждения.")
        return 0
    print(bold(f"Ждут утверждения ({config.REVIEW_DIR_NAME}):"))
    for i, batch in enumerate(batches, 1):
        present = batch.present()
        print(f"  {i}. {batch.name} — {objects(len(present))}, {human_size(batch.size())}")
        print(dim(f"     {batch.path}"))
    chosen = batches
    if interactive:
        answer = ask("Какие утвердить? Enter — все, номера через запятую, 0 — отмена: ")
        if answer is None or answer == "0":
            return 0
        if answer:
            numbers = _parse_numbers(answer, len(batches))
            if not numbers:
                print(red("Не понял номера."))
                return 1
            chosen = [batches[n - 1] for n in sorted(numbers)]
        if confirm("Открыть папки в Проводнике, чтобы посмотреть ещё раз?"):
            for batch in chosen:
                open_in_system(batch.path)
            ask("Когда закончишь (нужное — в «_ВЕРНУТЬ»), нажми Enter…")

    for batch in chosen:
        matches, unmatched = review._match_returned(batch, set())
        returning = {id(e) for _, e in matches}
        to_delete = [e for e in batch.present() if id(e) not in returning]
        size = sum(e.get("size", 0) for e in to_delete)
        print(bold(f"\n{batch.name}"))
        if matches:
            print(green(f"  Вернётся на место: {objects(len(matches))}"))
        if unmatched:
            print(yellow(f"  Не понял, куда вернуть {objects(len(unmatched))} из «_ВЕРНУТЬ» — останутся там:"))
            for path in unmatched[:5]:
                print(yellow(f"    {display(path)}"))
        print(f"  Удалится насовсем: {objects(len(to_delete))}, {human_size(size)}")
        if not to_delete and not matches:
            continue
        if not args.yes and not confirm("  Выполнить? Удаление нельзя отменить"):
            print("  Пропущено.")
            continue
        with journal.Session("approve", f"Утверждение: {batch.name}") as session:
            result = review.approve(batch, session)
        print(green(f"  Удалено {objects(result.deleted)}, освобождено {human_size(result.freed)}."))
        if result.restored:
            print(green(f"  Возвращено на места: {len(result.restored)}"))
        if result.kept_outside:
            print(dim(f"  Ты забрал сам: {objects(result.kept_outside)} — не трогал."))
        for error in result.errors[:10]:
            print(yellow(f"  ! {error}"))
    return 0


# ======================================================================= сортировка
def _choose_folder(rules: Rules, purpose: str) -> Path | None:
    options = rules.sort_folders()
    folders = config.user_folders()
    for extra in ("documents", "pictures", "videos"):
        path = folders.get(extra)
        if path and path not in options:
            options.append(path)
    print(bold(f"Какую папку {purpose}?"))
    for i, path in enumerate(options, 1):
        print(f"  {i}. {display(path)}")
    answer = ask("Номер или путь (Enter — 1, 0 — отмена): ")
    if answer is None or answer == "0":
        return None
    if answer == "":
        return options[0] if options else None
    if answer.isdigit() and 1 <= int(answer) <= len(options):
        return options[int(answer) - 1]
    path = Path(answer.strip('"')).expanduser()
    if not path.is_dir():
        print(red(f"Нет такой папки: {path}"))
        return None
    return path


def cmd_sort(args, rules: Rules, interactive: bool = False) -> int:
    folder = Path(args.folder).expanduser() if getattr(args, "folder", None) else None
    if interactive:
        folder = _choose_folder(rules, "разобрать")
        if folder is None:
            return 0
        if confirm("Раскладывать ещё и по годам (Документы/2024/…)?"):
            rules.data.setdefault("sort", {})["by_year"] = True
    elif getattr(args, "by_year", False):
        rules.data.setdefault("sort", {})["by_year"] = True
    if folder is None:
        folder = config.user_folders().get("downloads")
    if folder is None or not folder.is_dir():
        print(red("Не нашёл папку для сортировки."))
        return 1

    progress = Progress()
    print(bold(f"Сортировка: {display(folder)}"))
    ai = LocalAI(rules)
    if ai.enabled and rules.sectors and not ai.available():
        print(yellow(f"ИИ включён, но модель {ai.model} недоступна — запущен ли Ollama? "
                     "Раскладываю только по правилам."))
    plan = organizer.plan_sort(folder, rules, progress, ai=ai)
    progress.clear()
    if not plan.moves:
        print(green("Всё уже разложено — переносить нечего."))
        return 0

    by_label: dict[str, list[organizer.SortMove]] = defaultdict(list)
    for move in plan.moves:
        by_label["Дубликаты → на проверку" if move.duplicate_of else move.label].append(move)
    print(bold(f"\nПлан: {objects(len(plan.moves))}"))
    for label, moves in sorted(by_label.items(), key=lambda kv: -len(kv[1])):
        print("  " + line(label, sum(m.size for m in moves), objects(len(moves))))
        for move in moves[:2]:
            print(dim(f"      {move.src.name} — {move.reason}"))
    if plan.skipped:
        print(dim("  Не трогаю: " + ", ".join(f"{reason} ({n})" for reason, n in plan.skipped.most_common())))
    if plan.unknown:
        print(dim(f"  Неизвестные типы оставлю на месте: {files(len(plan.unknown))}"))
    remembered = [m for m in plan.moves if m.referenced_by]
    if remembered:
        apps = Counter(app for m in remembered for app in m.referenced_by)
        print(yellow(f"\n  Эти файлы помнят программы: " + ", ".join(f"{a} ({n})" for a, n in apps.most_common(8))))
        print(dim("  Office и ярлыки поправлю сам, для остальных на старом месте останется ссылка — "
                  "программы ничего не потеряют."))
    targets = {m.dst.drive.upper() for m in plan.moves} - {folder.drive.upper()}
    if targets:
        print(yellow(f"  Часть переедет на другой диск ({', '.join(sorted(targets))}) — это копирование, будет дольше."))

    rows = [[display(m.src), display(m.dst), m.reason] for m in plan.moves]
    path = report.save("сортировка", report.render(
        f"Сортировка: {display(folder)}", "План — ничего ещё не перенесено.",
        [("Переносов", str(len(plan.moves)))], [report.Section("Что куда", ["Откуда", "Куда", "Почему"], rows, open=True)],
    ))
    print(dim(f"\nПолный план: {path}"))
    if interactive:
        if confirm("Открыть план в браузере?"):
            open_in_system(path)
    elif not args.apply:
        print(cyan("Это был просмотр. Выполнить: filecleaner sort --apply"))
        return 0
    if not getattr(args, "yes", False) and not confirm("Разложить?"):
        print("Отменено, ничего не тронуто.")
        return 0
    with journal.Session("sort", f"Сортировка {folder.name}") as session:
        result = organizer.apply_sort(plan, rules, session, progress)
    progress.clear()
    print(green(f"Разложено: {objects(result.moved)}."))
    extras = []
    if result.links:
        extras.append(f"оставлено ссылок для программ: {result.links}")
    if result.office:
        extras.append(f"поправлено в «Недавних» Office: {result.office}")
    if result.shortcuts:
        extras.append(f"поправлено ярлыков: {result.shortcuts}")
    if result.duplicates_staged:
        extras.append(f"дубликатов на проверку: {result.duplicates_staged}")
    if extras:
        print("  " + "; ".join(extras))
    for error in result.errors[:10]:
        print(yellow(f"  ! {error}"))
    print(dim("Передумал? «filecleaner undo» вернёт всё как было."))
    return 0


# ======================================================================= сжатие
def cmd_compress(args, rules: Rules, interactive: bool = False) -> int:
    folder = Path(args.folder).expanduser() if getattr(args, "folder", None) else None
    if interactive:
        folder = _choose_folder(rules, "сжать")
        if folder is None:
            return 0
    if folder is None or not folder.is_dir():
        print(red("Укажи папку: filecleaner compress ПАПКА"))
        return 1
    progress = Progress()
    print(bold(f"Сжатие: {display(folder)}"))
    plan = compress.plan_compress(folder, rules, progress)
    progress.clear()
    if plan.skipped:
        print(dim("  Пропускаю: " + ", ".join(f"{reason} ({n})" for reason, n in plan.skipped.most_common())))
    if plan.vm_disks:
        size = sum(c.size for c in plan.vm_disks)
        gain = sum(c.size * c.saving for c in plan.vm_disks)
        print(yellow(f"  Диски виртуалок ({len(plan.vm_disks)}, {human_size(size)}) сжали бы ещё ~{human_size(gain)}, "
                     "но я их не трогаю: включи [compress] include_vm_disks, если виртуалка не используется."))
    if not plan.candidates:
        print(green("Сжимать нечего: всё уже сжато или не сжимается."))
        return 0
    total = sum(c.size for c in plan.candidates)
    by_ext: dict[str, list[float]] = defaultdict(lambda: [0, 0.0])
    for c in plan.candidates:
        entry = by_ext[c.path.suffix.lower() or "(без расширения)"]
        entry[0] += c.size
        entry[1] += c.size * c.saving
    print(bold(f"\nМожно сжать {files(len(plan.candidates))} ({human_size(total)}) → освободится примерно "
               f"{human_size(plan.expected)}"))
    for ext, (size, gain) in sorted(by_ext.items(), key=lambda kv: -kv[1][1])[:8]:
        print("  " + line(ext, int(gain), f"из {human_size(size)}", width=20))
    print(dim("Файлы останутся на местах и будут открываться как обычно."))
    if not interactive and not args.apply:
        print(cyan("Это был просмотр. Выполнить: filecleaner compress ПАПКА --apply"))
        return 0
    if not getattr(args, "yes", False) and not confirm("Сжать?"):
        return 0
    with journal.Session("compress", f"Сжатие {folder.name}") as session:
        result = compress.apply_compress(plan, rules, session, progress)
    progress.clear()
    print(green(f"Сжато {files(result.files)}: было {human_size(result.before)}, стало {human_size(result.after)} — "
                f"освобождено {human_size(result.saved)}."))
    for error in result.errors[:5]:
        print(yellow(f"  ! {error}"))
    return 0


# ======================================================================= история и отмена
def cmd_history(args, rules: Rules, interactive: bool = False) -> int:
    sessions = journal.list_sessions()[:20]
    if not sessions:
        print("История пуста.")
        return 0
    print(bold("История:"))
    for s in sessions:
        status = green("отменено") if s.undone else (cyan("можно отменить") if s.undoable else "")
        print(f"  {s.created:%d.%m %H:%M}  {s.title[:34]:<34} {s.summary()}  {status}")
        print(dim(f"                {s.id}"))
    return 0


def cmd_undo(args, rules: Rules, interactive: bool = False) -> int:
    candidates = [s for s in journal.list_sessions() if s.undoable]
    if not candidates:
        print("Отменять нечего.")
        return 0
    target = None
    wanted = getattr(args, "id", None)
    if interactive:
        for i, s in enumerate(candidates[:15], 1):
            print(f"  {i}. {s.created:%d.%m %H:%M}  {s.title} — {s.summary()}")
        answer = ask("Что отменить? Номер (Enter — 1, 0 — ничего): ")
        if answer is None or answer == "0":
            return 0
        index = int(answer) if answer.isdigit() else 1
        if not 1 <= index <= min(15, len(candidates)):
            print(red("Нет такого номера."))
            return 1
        target = candidates[index - 1]
    elif wanted in (None, "last"):
        target = candidates[0]
    else:
        target = next((s for s in candidates if s.id.startswith(wanted)), None)
        if target is None:
            print(red(f"Не нашёл «{wanted}» среди действий, которые можно отменить (см. filecleaner history)."))
            return 1
    deleted = len(target.ops("delete"))
    print(f"Отменяю: {target.title} от {target.created:%d.%m %H:%M} — {target.summary()}")
    if deleted:
        print(dim(f"Удалённое ({objects(deleted)}: кэши, временные файлы) не вернуть — остальное вернётся."))
    if not getattr(args, "yes", False) and not confirm("Отменить?"):
        return 0
    result = journal.undo(target)
    print(green(f"Возвращено: {objects(result.restored)}."))
    for path in result.missing[:10]:
        print(yellow(f"  ! Не нашёл {display(path)} — возможно, его уже убрали вручную."))
    for error in result.errors[:10]:
        print(yellow(f"  ! {error}"))
    return 0


# ======================================================================= правила
def cmd_rules(args, rules: Rules, interactive: bool = False) -> int:
    path = ensure_user_rules()
    print(f"Твои правила: {path}")
    titles = {"delete": "удалять сразу", "review": "на проверку", "report": "только отчёт", "off": "выключено"}
    from .rules import MODE_KEYS

    grouped: dict[str, list[str]] = defaultdict(list)
    for key in MODE_KEYS:
        grouped[rules.mode(key)].append(key)
    for mode in ("delete", "review", "report", "off"):
        if grouped[mode]:
            print(f"  {bold(titles[mode])}: " + ", ".join(grouped[mode]))
    sectors = ", ".join(s["name"] for s in rules.sectors) or "нет"
    print(f"  {bold('секторы')}: {sectors}")
    ai = LocalAI(rules)
    if not ai.enabled:
        status = "выключен ([ai] enabled = false)"
    elif ai.available():
        status = green(f"включён, модель {ai.model} готова")
    else:
        status = yellow(f"включён, но модель {ai.model} недоступна — запущен ли Ollama? (ollama pull {ai.model})")
    print(f"  {bold('ИИ')}: {status}")
    if getattr(args, "edit", False) or (interactive and confirm("Открыть правила в Блокноте?")):
        open_in_system(path)
    return 0


# ======================================================================= приступай
def _night_describe(rules: Rules) -> None:
    roots = night.night_roots(rules)
    dumps = analyzers.dump_folders(roots, rules)
    drives = [str(p) for name, p in roots.items() if fsutil.is_drive_root(p)]
    print(bold("Приступай — всё за один запуск, можно оставить на ночь:"))
    print(f"  1. Мусор и копии ищу {'на дисках ' + ', '.join(drives) if drives else 'в личных папках'} "
          f"(Windows, программы, игры и проекты не трогаю; на системном диске вне твоей папки — только отчёт).")
    print("  2. Кэши и временные файлы удаляю сразу.")
    if rules.get("night.auto_delete", True):
        print(f"  3. Точные копии и распакованные zip в свалках ({', '.join(display(d) for d in dumps)}) "
              "удаляю сразу — после сверки с оригиналом.")
    print(f"  4. Остальное — в «{config.REVIEW_DIR_NAME}», утром решаешь сам.")
    folders = night.sort_folders(roots, rules, dumps)
    print("  5. Раскладываю: " + ", ".join(display(p) + ("" if subs else " (только файлы)") for p, subs in folders))
    ai = LocalAI(rules)
    if ai.enabled:
        status = ("локальная модель " + ai.model) if ai.local else "ВЫКЛЮЧЕН: адрес модели не на этом компьютере"
        print(dim(f"     ИИ: {status}. Всё считается на этом компьютере, в интернет ничего не уходит."))
    after = {"nothing": "ничего не делаю", "sleep": "усыпляю компьютер", "shutdown": "выключаю компьютер"}
    print(f"  6. Потом {after[rules.get('night.after', 'nothing')]} (night.after в правилах).")


def _night_log(progress: Progress):
    path = config.DATA_DIR / "logs" / f"night-{time.strftime('%Y%m%d-%H%M')}.log"
    path.parent.mkdir(parents=True, exist_ok=True)

    def log(text: str) -> None:
        progress.clear()
        print(text, flush=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(text + "\n")

    return log, path


def cmd_night(args, rules: Rules, interactive: bool = False) -> int:
    if getattr(args, "after", None):
        rules.data.setdefault("night", {})["after"] = args.after
    _night_describe(rules)
    if not interactive and not args.apply:
        progress = Progress()
        print(bold("\nПросмотр: ищу, ничего не трогаю…"))
        plan = night.plan_night(rules, progress)
        progress.clear()
        junk = plan.check.by_mode("delete")
        print(f"  Кэши и временное: {human_size(sum(f.size for f in junk))}")
        print(f"  Удалю сразу (после сверки): {objects(len(plan.auto))}, {human_size(sum(f.size for f in plan.auto))}")
        for f in sorted(plan.auto, key=lambda f: -f.size)[:8]:
            print(dim(f"      {display(f.path)} — {f.reason}"))
        waiting = plan.morning + [f for f, _ in plan.held]
        print(f"  До утра в «{config.REVIEW_DIR_NAME}»: {objects(len(waiting))}, {human_size(sum(f.size for f in waiting))}")
        print(f"  Только в отчёт: {human_size(sum(f.size for f in plan.check.by_mode('report')))}")
        print(cyan("Это был просмотр. Выполнить: filecleaner night --apply"))
        return 0
    if not getattr(args, "yes", False) and not confirm("\nПриступить? Дальше можно уйти — всё сделается само"):
        print("Отменено, ничего не тронуто.")
        return 0

    progress = Progress()
    log, log_path = _night_log(progress)
    out = night.run_night(rules, log, progress)
    progress.clear()
    moved = sum(r.moved for _, _, r in out.sorted)
    print(green(f"\nГотово: освобождено {human_size(out.junk_freed + out.auto_freed)} "
                f"(кэши {human_size(out.junk_freed)}, проверенные копии {human_size(out.auto_freed)}), "
                f"разложено {objects(moved)}."))
    if out.waiting:
        print(yellow(f"Ждёт утра: {objects(out.waiting)}, {human_size(out.waiting_bytes)} — "
                     f"«{config.REVIEW_DIR_NAME}», потом «Утвердить удаление»."))
    for note in out.notes[:10]:
        print(dim(f"  {note}"))
    for error in out.errors[:10]:
        print(yellow(f"  ! {error}"))
    print(dim(f"Отчёт: {out.report}\nЖурнал: {log_path}"))
    night.after(rules.get("night.after", "nothing"), log)
    return 0


# ======================================================================= меню
MENU = [
    ("1", "Приступай: чистка всех дисков и сортировка за один раз (можно на ночь)", cmd_night),
    ("2", "Скан: что занимает место", cmd_scan),
    ("3", "Проверка: мусор, дубли и лишнее → «Ready for approval»", cmd_check),
    ("4", "Сортировка: разложить файлы по папкам", cmd_sort),
    ("5", "Утвердить удаление (папка «Ready for approval»)", cmd_approve),
    ("6", "Сжатие: освободить место без удаления", cmd_compress),
    ("7", "История", cmd_history),
    ("8", "Отменить действие", cmd_undo),
    ("9", "Правила: что считать ненужным", cmd_rules),
]


def menu() -> int:
    print(bold(f"File Cleaner {__version__}") + dim(" — порядок на диске, удаление только с твоего подтверждения"))
    last = night.last_run()
    if last:
        print(dim(f"Последний «Приступай»: {last['finished'].replace('T', ' ')} — освобождено "
                  f"{human_size(last['freed'])}, разложено {last['sorted']}. Отчёт: {last['report']}"))
    waiting = review.find_batches()
    if waiting:
        size = sum(b.size() for b in waiting)
        print(yellow(f"В «{config.REVIEW_DIR_NAME}» ждёт решения {human_size(size)} — пункт 5."))
    while True:
        print()
        for key, title, _ in MENU:
            print(f"  {key}  {title}")
        print("  0  Выход")
        choice = ask("Пункт: ")
        if choice is None or choice in ("0", "q", "й"):
            return 0
        action = next((fn for key, _, fn in MENU if key == choice), None)
        if action is None:
            continue
        try:
            rules = Rules.load()
        except RulesError as exc:
            print(red(str(exc)))
            continue
        print()
        try:
            action(argparse.Namespace(apply=False, yes=False), rules, interactive=True)
        except KeyboardInterrupt:
            print(yellow("\nПрервано."))


# ======================================================================= запуск
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="filecleaner",
        description="Скан, проверка, сортировка и удаление ненужного — только с твоего подтверждения. "
                    "Без аргументов открывается меню.",
    )
    parser.add_argument("--version", action="version", version=f"File Cleaner {__version__}")
    parser.add_argument("--rules", help="свой файл правил вместо стандартного")
    sub = parser.add_subparsers(dest="command", metavar="КОМАНДА")

    def add(name: str, func, help_text: str) -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=help_text, description=help_text)
        p.set_defaults(func=func)
        return p

    p = add("night", cmd_night, "приступай: чистка всех дисков и сортировка за один раз — можно на ночь "
                                "(без --apply только показать)")
    p.add_argument("--apply", action="store_true", help="выполнить, а не только показать")
    p.add_argument("--yes", action="store_true", help="не спрашивать подтверждение")
    p.add_argument("--after", choices=night.AFTER, help="что сделать в конце (по умолчанию — night.after в правилах)")
    add("scan", cmd_scan, "что занимает место")
    p = add("check", cmd_check, "найти мусор, дубли и лишнее (с --apply: кэши удалить, остальное — на проверку)")
    p.add_argument("--apply", action="store_true", help="выполнить, а не только показать")
    p.add_argument("--yes", action="store_true", help="не спрашивать подтверждение")
    p = add("approve", cmd_approve, "утвердить: вернуть отмеченное, удалить остальное из «Ready for approval»")
    p.add_argument("--yes", action="store_true", help="не спрашивать подтверждение")
    p = add("sort", cmd_sort, "разложить файлы папки по секторам и типам")
    p.add_argument("folder", nargs="?", help="папка (по умолчанию Загрузки)")
    p.add_argument("--by-year", action="store_true", help="ещё и по годам")
    p.add_argument("--apply", action="store_true", help="выполнить, а не только показать")
    p.add_argument("--yes", action="store_true", help="не спрашивать подтверждение")
    p = add("compress", cmd_compress, "сжать папку прозрачным сжатием Windows")
    p.add_argument("folder", help="папка")
    p.add_argument("--apply", action="store_true", help="выполнить, а не только показать")
    p.add_argument("--yes", action="store_true", help="не спрашивать подтверждение")
    add("history", cmd_history, "что программа уже делала")
    p = add("undo", cmd_undo, "отменить действие (по умолчанию последнее)")
    p.add_argument("id", nargs="?", help="номер из history или last")
    p.add_argument("--yes", action="store_true", help="не спрашивать подтверждение")
    p = add("rules", cmd_rules, "показать правила; --edit — открыть их для правки")
    p.add_argument("--edit", action="store_true", help="открыть файл правил")
    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream.isatty():
                stream.reconfigure(errors="replace")
            else:  # вывод в файл или другую программу — UTF-8, чтобы не терялась кириллица
                stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    args = build_parser().parse_args(argv)
    if args.command is None:
        try:
            return menu()
        except KeyboardInterrupt:
            return 130
    try:
        rules = Rules.load(Path(args.rules) if args.rules else None)
    except RulesError as exc:
        print(red(str(exc)))
        return 2
    try:
        return args.func(args, rules) or 0
    except KeyboardInterrupt:
        print(yellow("\nПрервано."))
        return 130
