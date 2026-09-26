"""Интерфейс: команды для терминала и меню (запуск без аргументов) — на языке программы, русском или английском."""
from __future__ import annotations

import argparse
import os
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

from . import (__version__, analyzers, compress, config, fsutil, i18n, journal, licensing, night, organizer, pipeline,
               report, review)
from .ai import LocalAI
from .fsutil import display, human_size, plural
from .i18n import tr
from .index import Index
from .rules import Rules, RulesError, ensure_user_rules

# ======================================================================= вывод
for _name in ("stdout", "stderr"):
    if getattr(sys, _name) is None:  # без консоли (окно, запуск из Планировщика) потоков вывода нет вовсе
        setattr(sys, _name, open(os.devnull, "w", encoding="utf-8"))
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


# Ответ «да» понимается на обоих языках, какой бы ни был на экране (т, так — по-украински).
YES = {"д", "да", "y", "yes", "т", "так"}


def ask(prompt: str) -> str | None:
    """Ответ пользователя; None — если ввод закрыт или нажат Ctrl+C."""
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None


def confirm(prompt: str) -> bool:
    answer = ask(f"{prompt} {tr('[д/н]')}: ")
    return (answer or "").lower() in YES


def open_in_system(path: Path) -> None:
    try:
        os.startfile(path)  # type: ignore[attr-defined]
    except (AttributeError, OSError) as exc:
        print(red(tr("Не получилось открыть {path}: {error}", path=path, error=exc)))


def line(title: str, size: int, count: str = "", width: int = 44) -> str:
    return f"{title[:width]:<{width}} {human_size(size):>10}  {dim(count)}"


def folder_title(name: str) -> str:
    """Личная папка: «downloads» → «Загрузки» (по-английски — «Downloads»); свой путь — как есть."""
    if name not in config.FOLDER_TITLES:
        return name
    return name.capitalize() if i18n.language() == "en" else config.FOLDER_TITLES[name]


# ======================================================================= скан
def cmd_scan(args, rules: Rules, interactive: bool = False) -> int:
    roots = rules.roots()
    print(bold(tr("Скан: что занимает место")))
    for name, path in roots.items():
        print(dim(f"  {folder_title(name)}: {display(path)}"))
    progress = Progress()
    with Index() as index:
        stats = index.scan(roots, progress)
        recs = index.files(roots.keys())
    progress.clear()
    print("\n" + tr("В индексе {files} на {size} (скан {seconds} с).", files=files(stats.files),
                    size=human_size(stats.bytes), seconds=f"{stats.seconds:.0f}"))
    if stats.cloud:
        print(dim(tr("Облачных файлов OneDrive, которых нет на диске: {count} — их не трогаю.", count=stats.cloud)))

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
            top = root / rel.parts[0] if len(rel.parts) > 1 else root / tr("(файлы в корне)")
            by_folder[str(top)] += rec.size

    print(bold("\n" + tr("По папкам:")))
    for name, (n, size) in sorted(by_root.items(), key=lambda kv: -kv[1][1]):
        print("  " + line(folder_title(name), size, files(n)))
    print(bold("\n" + tr("По типам:")))
    for kind, (n, size) in sorted(by_type.items(), key=lambda kv: -kv[1][1])[:12]:
        print("  " + line(i18n.type_name(kind), size, files(n)))
    print(bold("\n" + tr("Самые большие подпапки:")))
    for folder, size in sorted(by_folder.items(), key=lambda kv: -kv[1])[:10]:
        print("  " + line(display(folder), size, width=60))
    print(bold("\n" + tr("Самые большие файлы:")))
    for rec in sorted(local, key=lambda r: -r.size)[:10]:
        print("  " + line(display(rec.path), rec.size, width=60))
    print(dim("\n" + tr("Дальше: «filecleaner check» — найдёт мусор, дубли и лишнее.")))
    return 0


# ======================================================================= проверка
def mode_title(mode: str, short: bool = False) -> str:
    """Что будет с группой: «Удалю сразу — …»; short — только начало («Удалю сразу»)."""
    if short:
        return {"delete": tr("Удалю сразу"), "review": tr("На проверку"), "report": tr("Только в отчёте")}[mode]
    return {
        "delete": tr("Удалю сразу — это восстанавливается само"),
        "review": tr("На проверку → папка «{folder}» (удалится только после твоего «утвердить»)",
                     folder=config.REVIEW_DIR_NAME),
        "report": tr("Только в отчёте — решаешь сам, программа не трогает"),
    }[mode]


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
        print(bold(f"\n{mode_title(mode)}: {human_size(total)}"))
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
        print(bold("\n" + tr("Замечания:")))
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
        title = f"{group} — {size} · {mode_title(mode, short=True).lower()}"
        if mode == "delete" and len(items) > 50:
            by_dir: dict[str, list[int]] = defaultdict(lambda: [0, 0])
            for f in items:
                entry = by_dir[str(f.path.parent)]
                entry[0] += 1
                entry[1] += f.size
            rows = [[display(d), str(n), human_size(s)] for d, (n, s) in sorted(by_dir.items(), key=lambda kv: -kv[1][1])]
            sections.append(report.Section(title, [tr("Папка"), tr("Файлов"), tr("Размер")], rows,
                                           note=objects(len(items))))
        else:
            rows = [[display(f.path), human_size(f.size), f.reason] for f in sorted(items, key=lambda f: -f.size)]
            sections.append(report.Section(title, [tr("Файл"), tr("Размер"), tr("Почему")], rows,
                                           note=objects(len(items))))
    cards = [(mode_title(m, short=True), human_size(sum(f.size for f in result.by_mode(m))))
             for m in ("delete", "review", "report") if result.by_mode(m)]
    return report.render(tr("Проверка: мусор, дубли и лишнее"), tr("Ничего не удалено — это только отчёт."), cards,
                         sections)


def _parse_numbers(answer: str, limit: int) -> set[int] | None:
    numbers = set()
    for part in answer.replace(" ", "").split(","):
        if not part:
            continue
        if not part.isdigit() or not 1 <= int(part) <= limit:
            return None
        numbers.add(int(part))
    return numbers


def _license_line(info: dict) -> str:
    if info["state"] == "licensed":
        return tr("Лицензия на имя: {name}.", name=info["name"])
    if info["state"] == "trial":
        return tr("Пробный период: осталось {days}.", days=plural(info["days_left"], "день", "дня", "дней"))
    return tr("Пробный период закончился. Ввести ключ: filecleaner license FC1-…")


def _licensed(unattended: bool = False) -> bool:
    """Чистить, раскладывать и удалять можно в пробный период и с ключом; смотреть — всегда."""
    try:
        licensing.require()
    except licensing.LicenseError as exc:
        print(red(str(exc)))
        if unattended:  # ночной запуск из Планировщика: окна нет — причина остаётся в журнале
            (config.DATA_DIR / "logs").mkdir(parents=True, exist_ok=True)
            with open(config.DATA_DIR / "logs" / "night-errors.log", "a", encoding="utf-8") as fh:
                fh.write(f"--- {time.strftime('%Y-%m-%d %H:%M')} {exc}\n")
        return False
    return True


def cmd_check(args, rules: Rules, interactive: bool = False) -> int:
    roots = rules.roots()
    progress = Progress()
    print(bold(tr("Проверка: ищу мусор, дубли и лишнее…")))
    with Index() as index:
        index.scan(roots, progress)
        result = analyzers.run_check(index, roots, rules, progress)
    progress.clear()
    if not result.findings:
        print(green(tr("Ничего лишнего не нашёл.")))
        return 0
    numbered = _print_check(result)
    path = report.save(tr("проверка"), _check_report(result))
    print(dim("\n" + tr("Подробный отчёт: {path}", path=path)))
    if interactive and confirm(tr("Открыть отчёт в браузере?")):
        open_in_system(path)
    if not numbered:
        print(tr("Действовать нечего: всё найденное — только для отчёта."))
        return 0

    skip: set[str] = set()
    if (interactive or getattr(args, "apply", False)) and not _licensed():
        return 3
    if interactive:
        while True:
            answer = ask("\n" + tr("Выполнить? Enter — да; номера групп через запятую — пропустить их; 0 — отмена: "))
            if answer is None or answer == "0":
                print(tr("Отменено, ничего не тронуто."))
                return 0
            numbers = _parse_numbers(answer, len(numbered))
            if numbers is None:
                print(red(tr("Не понял. Номера от 1 до {count} через запятую.", count=len(numbered))))
                continue
            skip = {numbered[n - 1] for n in numbers}
            break
    elif not args.apply:
        print(cyan("\n" + tr("Это был просмотр. Выполнить: filecleaner check --apply")))
        return 0
    elif not args.yes and not confirm(tr("Удалить кэши и перенести остальное на проверку?")):
        print(tr("Отменено, ничего не тронуто."))
        return 0

    with journal.Session("check", tr("Проверка")) as session:
        out = pipeline.apply_check(result, session, skip, progress)
    progress.clear()
    if out.deleted:
        print(green(tr("Удалено {count}, освобождено {size}.", count=objects(out.deleted), size=human_size(out.freed))))
    if out.staged:
        print(green(tr("На проверку перенесено {count} ({size}):", count=objects(out.staged),
                       size=human_size(out.staged_bytes))))
        for batch in out.batches:
            print(f"  {batch.path}")
        print(tr("Загляни туда: нужное перетащи в «{ret}», потом «filecleaner approve» удалит остальное.",
                 ret=review.return_dir_name()))
    for error in out.errors[:10]:
        print(yellow(f"  ! {error}"))
    if len(out.errors) > 10:
        print(yellow("  " + tr("…и ещё {count} ошибок (обычно — файл открыт в программе).",
                               count=len(out.errors) - 10)))
    if out.busy:
        print(dim(tr("Пропущено {files} кэшей и временных: заняты программами или нужны права администратора.",
                     files=files(out.busy))))
    print(dim(tr("Передумал? «filecleaner undo» вернёт перенесённое на места.")))
    return 0


# ======================================================================= утверждение
def cmd_approve(args, rules: Rules, interactive: bool = False) -> int:
    batches = review.find_batches()
    if not batches:
        print(tr("В «{folder}» ничего не ждёт утверждения.", folder=config.REVIEW_DIR_NAME))
        return 0
    print(bold(tr("Ждут утверждения ({folder}):", folder=config.REVIEW_DIR_NAME)))
    for i, batch in enumerate(batches, 1):
        present = batch.present()
        print(f"  {i}. {batch.name} — {objects(len(present))}, {human_size(batch.size())}")
        print(dim(f"     {batch.path}"))
    if not _licensed():
        return 3
    chosen = batches
    if interactive:
        answer = ask(tr("Какие утвердить? Enter — все, номера через запятую, 0 — отмена: "))
        if answer is None or answer == "0":
            return 0
        if answer:
            numbers = _parse_numbers(answer, len(batches))
            if not numbers:
                print(red(tr("Не понял номера.")))
                return 1
            chosen = [batches[n - 1] for n in sorted(numbers)]
        if confirm(tr("Открыть папки в Проводнике, чтобы посмотреть ещё раз?")):
            for batch in chosen:
                open_in_system(batch.path)
            ask(tr("Когда закончишь (нужное — в «{ret}»), нажми Enter…", ret=review.return_dir_name()))

    for batch in chosen:
        matches, unmatched = review._match_returned(batch, set())
        returning = {id(e) for _, e in matches}
        to_delete = [e for e in batch.present() if id(e) not in returning]
        size = sum(e.get("size", 0) for e in to_delete)
        print(bold(f"\n{batch.name}"))
        if matches:
            print(green("  " + tr("Вернётся на место: {count}", count=objects(len(matches)))))
        if unmatched:
            print(yellow("  " + tr("Не понял, куда вернуть {count} из «{ret}» — останутся там:",
                                   count=objects(len(unmatched)), ret=review.return_dir_name())))
            for path in unmatched[:5]:
                print(yellow(f"    {display(path)}"))
        print("  " + tr("Удалится насовсем: {count}, {size}", count=objects(len(to_delete)), size=human_size(size)))
        if not to_delete and not matches:
            continue
        if not args.yes and not confirm("  " + tr("Выполнить? Удаление нельзя отменить")):
            print("  " + tr("Пропущено."))
            continue
        with journal.Session("approve", tr("Утверждение: {name}", name=batch.name)) as session:
            result = review.approve(batch, session)
        print(green("  " + tr("Удалено {count}, освобождено {size}.", count=objects(result.deleted),
                              size=human_size(result.freed))))
        if result.restored:
            print(green("  " + tr("Возвращено на места: {count}", count=len(result.restored))))
        if result.kept_outside:
            print(dim("  " + tr("Ты забрал сам: {count} — не трогал.", count=objects(result.kept_outside))))
        for error in result.errors[:10]:
            print(yellow(f"  ! {error}"))
    return 0


# ======================================================================= сортировка
def _choose_folder(rules: Rules, question: str) -> Path | None:
    options = rules.sort_folders()
    folders = config.user_folders()
    for extra in ("documents", "pictures", "videos"):
        path = folders.get(extra)
        if path and path not in options:
            options.append(path)
    print(bold(question))
    for i, path in enumerate(options, 1):
        print(f"  {i}. {display(path)}")
    answer = ask(tr("Номер или путь (Enter — 1, 0 — отмена): "))
    if answer is None or answer == "0":
        return None
    if answer == "":
        return options[0] if options else None
    if answer.isdigit() and 1 <= int(answer) <= len(options):
        return options[int(answer) - 1]
    path = Path(answer.strip('"')).expanduser()
    if not path.is_dir():
        print(red(tr("Нет такой папки: {path}", path=path)))
        return None
    return path


def cmd_sort(args, rules: Rules, interactive: bool = False) -> int:
    folder = Path(args.folder).expanduser() if getattr(args, "folder", None) else None
    if interactive:
        folder = _choose_folder(rules, tr("Какую папку разобрать?"))
        if folder is None:
            return 0
        if confirm(tr("Раскладывать ещё и по годам (Документы/2024/…)?")):
            rules.data.setdefault("sort", {})["by_year"] = True
    elif getattr(args, "by_year", False):
        rules.data.setdefault("sort", {})["by_year"] = True
    if folder is None:
        folder = config.user_folders().get("downloads")
    if folder is None or not folder.is_dir():
        print(red(tr("Не нашёл папку для сортировки.")))
        return 1

    progress = Progress()
    print(bold(tr("Сортировка: {folder}", folder=display(folder))))
    ai = LocalAI(rules)
    if ai.enabled and rules.sectors and not ai.available():
        print(yellow(tr("ИИ включён, но модель {model} недоступна — запущен ли Ollama? "
                        "Раскладываю только по правилам.", model=ai.model)))
    plan = organizer.plan_sort(folder, rules, progress, ai=ai)
    progress.clear()
    if not plan.moves:
        print(green(tr("Всё уже разложено — переносить нечего.")))
        return 0

    by_label: dict[str, list[organizer.SortMove]] = defaultdict(list)
    for move in plan.moves:
        by_label[tr("Дубликаты → на проверку") if move.duplicate_of else move.label].append(move)
    print(bold("\n" + tr("План: {count}", count=objects(len(plan.moves)))))
    for label, moves in sorted(by_label.items(), key=lambda kv: -len(kv[1])):
        print("  " + line(label, sum(m.size for m in moves), objects(len(moves))))
        for move in moves[:2]:
            print(dim(f"      {move.src.name} — {move.reason}"))
    if plan.skipped:
        print(dim("  " + tr("Не трогаю: {reasons}", reasons=", ".join(
            f"{reason} ({n})" for reason, n in plan.skipped.most_common()))))
    if plan.unknown:
        print(dim("  " + tr("Неизвестные типы оставлю на месте: {files}", files=files(len(plan.unknown)))))
    remembered = [m for m in plan.moves if m.referenced_by]
    if remembered:
        apps = Counter(app for m in remembered for app in m.referenced_by)
        print(yellow("\n  " + tr("Эти файлы помнят программы: {apps}", apps=", ".join(
            f"{a} ({n})" for a, n in apps.most_common(8)))))
        print(dim("  " + tr("Office и ярлыки поправлю сам, для остальных на старом месте останется ссылка — "
                            "программы ничего не потеряют.")))
    targets = {m.dst.drive.upper() for m in plan.moves} - {folder.drive.upper()}
    if targets:
        print(yellow("  " + tr("Часть переедет на другой диск ({drives}) — это копирование, будет дольше.",
                               drives=", ".join(sorted(targets)))))

    rows = [[display(m.src), display(m.dst), m.reason] for m in plan.moves]
    path = report.save(tr("сортировка"), report.render(
        tr("Сортировка: {folder}", folder=display(folder)), tr("План — ничего ещё не перенесено."),
        [(tr("Переносов"), str(len(plan.moves)))],
        [report.Section(tr("Что куда"), [tr("Откуда"), tr("Куда"), tr("Почему")], rows, open=True)],
    ))
    print(dim("\n" + tr("Полный план: {path}", path=path)))
    if interactive:
        if confirm(tr("Открыть план в браузере?")):
            open_in_system(path)
    elif not args.apply:
        print(cyan(tr("Это был просмотр. Выполнить: filecleaner sort --apply")))
        return 0
    if not _licensed():
        return 3
    if not getattr(args, "yes", False) and not confirm(tr("Разложить?")):
        print(tr("Отменено, ничего не тронуто."))
        return 0
    with journal.Session("sort", tr("Сортировка {folder}", folder=folder.name)) as session:
        result = organizer.apply_sort(plan, rules, session, progress)
    progress.clear()
    print(green(tr("Разложено: {count}.", count=objects(result.moved))))
    extras = []
    if result.links:
        extras.append(tr("оставлено ссылок для программ: {count}", count=result.links))
    if result.office:
        extras.append(tr("поправлено в «Недавних» Office: {count}", count=result.office))
    if result.shortcuts:
        extras.append(tr("поправлено ярлыков: {count}", count=result.shortcuts))
    if result.duplicates_staged:
        extras.append(tr("дубликатов на проверку: {count}", count=result.duplicates_staged))
    if extras:
        print("  " + "; ".join(extras))
    for error in result.errors[:10]:
        print(yellow(f"  ! {error}"))
    print(dim(tr("Передумал? «filecleaner undo» вернёт всё как было.")))
    return 0


# ======================================================================= сжатие
def cmd_compress(args, rules: Rules, interactive: bool = False) -> int:
    folder = Path(args.folder).expanduser() if getattr(args, "folder", None) else None
    if interactive:
        folder = _choose_folder(rules, tr("Какую папку сжать?"))
        if folder is None:
            return 0
    if folder is None or not folder.is_dir():
        print(red(tr("Укажи папку: filecleaner compress ПАПКА")))
        return 1
    progress = Progress()
    print(bold(tr("Сжатие: {folder}", folder=display(folder))))
    plan = compress.plan_compress(folder, rules, progress)
    progress.clear()
    if plan.skipped:
        print(dim("  " + tr("Пропускаю: {reasons}", reasons=", ".join(
            f"{reason} ({n})" for reason, n in plan.skipped.most_common()))))
    if plan.vm_disks:
        size = sum(c.size for c in plan.vm_disks)
        gain = sum(c.size * c.saving for c in plan.vm_disks)
        print(yellow("  " + tr("Диски виртуалок ({count}, {size}) сжали бы ещё ~{gain}, но я их не трогаю: "
                               "включи [compress] include_vm_disks, если виртуалка не используется.",
                               count=len(plan.vm_disks), size=human_size(size), gain=human_size(gain))))
    if not plan.candidates:
        print(green(tr("Сжимать нечего: всё уже сжато или не сжимается.")))
        return 0
    total = sum(c.size for c in plan.candidates)
    by_ext: dict[str, list[float]] = defaultdict(lambda: [0, 0.0])
    for c in plan.candidates:
        entry = by_ext[c.path.suffix.lower() or tr("(без расширения)")]
        entry[0] += c.size
        entry[1] += c.size * c.saving
    print(bold("\n" + tr("Можно сжать {files} ({size}) → освободится примерно {saved}",
                         files=files(len(plan.candidates)), size=human_size(total), saved=human_size(plan.expected))))
    for ext, (size, gain) in sorted(by_ext.items(), key=lambda kv: -kv[1][1])[:8]:
        print("  " + line(ext, int(gain), tr("из {size}", size=human_size(size)), width=20))
    print(dim(tr("Файлы останутся на местах и будут открываться как обычно.")))
    if not interactive and not args.apply:
        print(cyan(tr("Это был просмотр. Выполнить: filecleaner compress ПАПКА --apply")))
        return 0
    if not _licensed():
        return 3
    if not getattr(args, "yes", False) and not confirm(tr("Сжать?")):
        return 0
    with journal.Session("compress", tr("Сжатие {folder}", folder=folder.name)) as session:
        result = compress.apply_compress(plan, rules, session, progress)
    progress.clear()
    print(green(tr("Сжато {files}: было {before}, стало {after} — освобождено {saved}.", files=files(result.files),
                   before=human_size(result.before), after=human_size(result.after), saved=human_size(result.saved))))
    for error in result.errors[:5]:
        print(yellow(f"  ! {error}"))
    return 0


# ======================================================================= история и отмена
def cmd_history(args, rules: Rules, interactive: bool = False) -> int:
    sessions = journal.list_sessions()[:20]
    if not sessions:
        print(tr("История пуста."))
        return 0
    print(bold(tr("История:")))
    for s in sessions:
        status = green(tr("отменено")) if s.undone else (cyan(tr("можно отменить")) if s.undoable else "")
        title = i18n.retranslate(s.title)  # записано на языке, который был тогда
        print(f"  {s.created:%d.%m %H:%M}  {title[:34]:<34} {s.summary()}  {status}")
        print(dim(f"                {s.id}"))
    return 0


def cmd_undo(args, rules: Rules, interactive: bool = False) -> int:
    candidates = [s for s in journal.list_sessions() if s.undoable]
    if not candidates:
        print(tr("Отменять нечего."))
        return 0
    target = None
    wanted = getattr(args, "id", None)
    if interactive:
        for i, s in enumerate(candidates[:15], 1):
            print(f"  {i}. {s.created:%d.%m %H:%M}  {i18n.retranslate(s.title)} — {s.summary()}")
        answer = ask(tr("Что отменить? Номер (Enter — 1, 0 — ничего): "))
        if answer is None or answer == "0":
            return 0
        index = int(answer) if answer.isdigit() else 1
        if not 1 <= index <= min(15, len(candidates)):
            print(red(tr("Нет такого номера.")))
            return 1
        target = candidates[index - 1]
    elif wanted in (None, "last"):
        target = candidates[0]
    else:
        target = next((s for s in candidates if s.id.startswith(wanted)), None)
        if target is None:
            print(red(tr("Не нашёл «{id}» среди действий, которые можно отменить (см. filecleaner history).",
                         id=wanted)))
            return 1
    deleted = len(target.ops("delete"))
    print(tr("Отменяю: {title} от {date} — {summary}", title=i18n.retranslate(target.title),
             date=f"{target.created:%d.%m %H:%M}", summary=target.summary()))
    if deleted:
        print(dim(tr("Удалённое ({count}: кэши, временные файлы) не вернуть — остальное вернётся.",
                     count=objects(deleted))))
    if not getattr(args, "yes", False) and not confirm(tr("Отменить?")):
        return 0
    result = journal.undo(target)
    print(green(tr("Возвращено: {count}.", count=objects(result.restored))))
    for path in result.missing[:10]:
        print(yellow("  ! " + tr("Не нашёл {path} — возможно, его уже убрали вручную.", path=display(path))))
    for error in result.errors[:10]:
        print(yellow(f"  ! {error}"))
    return 0


# ======================================================================= правила
def cmd_rules(args, rules: Rules, interactive: bool = False) -> int:
    path = ensure_user_rules()
    print(tr("Твои правила: {path}", path=path))
    titles = {"delete": tr("удалять сразу"), "review": tr("на проверку"), "report": tr("только отчёт"),
              "off": tr("выключено")}
    from .rules import MODE_KEYS

    grouped: dict[str, list[str]] = defaultdict(list)
    for key in MODE_KEYS:
        grouped[rules.mode(key)].append(key)
    for mode in ("delete", "review", "report", "off"):
        if grouped[mode]:
            print(f"  {bold(titles[mode])}: " + ", ".join(grouped[mode]))
    sectors = ", ".join(s["name"] for s in rules.sectors) or tr("нет")
    print(f"  {bold(tr('секторы'))}: {sectors}")
    ai = LocalAI(rules)
    if not ai.enabled:
        status = tr("выключен ([ai] enabled = false)")
    elif ai.available():
        status = green(tr("включён, модель {model} готова", model=ai.model))
    else:
        status = yellow(tr("включён, но модель {model} недоступна — запущен ли Ollama? (ollama pull {model})",
                           model=ai.model))
    print(f"  {bold(tr('ИИ'))}: {status}")
    if getattr(args, "edit", False) or (interactive and confirm(tr("Открыть правила в Блокноте?"))):
        open_in_system(path)
    return 0


# ======================================================================= приступай
def _night_describe(rules: Rules) -> None:
    roots = night.night_roots(rules)
    dumps = analyzers.dump_folders(roots, rules)
    drives = [str(p) for name, p in roots.items() if fsutil.is_drive_root(p)]
    print(bold(tr("Приступай — всё за один запуск, можно оставить на ночь:")))
    where = tr("на дисках {drives}", drives=", ".join(drives)) if drives else tr("в личных папках")
    print("  1. " + tr("Мусор и копии ищу {where} (Windows, программы, игры и проекты не трогаю; "
                       "на системном диске вне твоей папки — только отчёт).", where=where))
    print("  2. " + tr("Кэши и временные файлы удаляю сразу."))
    if rules.get("night.auto_delete", True):
        print("  3. " + tr("Точные копии и распакованные zip в свалках ({folders}) удаляю сразу — "
                           "после сверки с оригиналом.", folders=", ".join(display(d) for d in dumps)))
    print("  4. " + tr("Остальное — в «{folder}», утром решаешь сам.", folder=config.REVIEW_DIR_NAME))
    folders = night.sort_folders(roots, rules, dumps)
    print("  5. " + tr("Раскладываю: {folders}", folders=", ".join(
        display(p) + ("" if subs else tr(" (только файлы)")) for p, subs in folders)))
    ai = LocalAI(rules)
    if ai.enabled:
        status = tr("локальная модель {model}", model=ai.model) if ai.local \
            else tr("ВЫКЛЮЧЕН: адрес модели не на этом компьютере")
        print(dim("     " + tr("ИИ: {status}. Всё считается на этом компьютере, в интернет ничего не уходит.",
                               status=status)))
    after = {"nothing": tr("ничего не делаю"), "sleep": tr("усыпляю компьютер"), "shutdown": tr("выключаю компьютер")}
    print("  6. " + tr("Потом {after} (night.after в правилах).", after=after[rules.get("night.after", "nothing")]))


def _night_log(progress: Progress):
    path = config.DATA_DIR / "logs" / f"night-{time.strftime('%Y%m%d-%H%M')}.log"
    path.parent.mkdir(parents=True, exist_ok=True)

    def log(text: str) -> None:
        progress.clear()
        print(text, flush=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(text + "\n")

    return log, path


def cmd_gui(args, rules: Rules, interactive: bool = False) -> int:
    from .gui.app import run  # окно подгружается, только когда его открывают

    path = Path(args.rules) if getattr(args, "rules", None) else None
    return run(lambda: Rules.load(path), port=args.port, show_window=not args.no_window)


def cmd_license(args, rules: Rules, interactive: bool = False) -> int:
    if args.key:
        try:
            info = licensing.activate(" ".join(args.key))
        except licensing.LicenseError as exc:
            print(red(str(exc)))
            return 1
        print(green(tr("Ключ принят, спасибо! {line}", line=_license_line(info))))
        return 0
    info = licensing.status()
    print(_license_line(info))
    if info["state"] != "licensed" and info["buy_url"]:
        print(tr("Купить ключ: {url}", url=info["buy_url"]))
    return 0


def cmd_update(args, rules: Rules, interactive: bool = False) -> int:
    from . import update

    try:
        latest = update.check(rules, force=True)
    except update.Unreachable:
        print(red(tr("GitHub не ответил — проверь интернет и попробуй ещё раз.")))
        return 1
    if not latest:
        print(green(tr("У тебя последняя версия ({version}).", version=__version__)))
        return 0
    print(yellow(tr("Вышла версия {version} (у тебя {current}).", version=latest["version"], current=__version__)))
    print(tr("Скачать: {url}", url=latest["url"]))
    return 0


def cmd_schedule(args, rules: Rules, interactive: bool = False) -> int:
    from . import schedule

    try:
        if args.off:
            schedule.remove()
            print(green(tr("Ночной запуск выключен.")))
        elif args.at:
            schedule.install(args.at, not args.no_wake)
    except schedule.ScheduleError as exc:
        print(red(str(exc)))
        return 1
    state = schedule.status()
    if state["enabled"]:
        wake = tr(", будит компьютер") if state["wake"] else ""
        print(tr("«Приступай» запускается сам каждую ночь в {time}{wake}.", time=state["time"], wake=wake))
    else:
        print(tr("Ночной запуск выключен. Включить: filecleaner schedule --at 03:00"))
    return 0


def cmd_night(args, rules: Rules, interactive: bool = False) -> int:
    if getattr(args, "after", None):
        rules.data.setdefault("night", {})["after"] = args.after
    _night_describe(rules)
    if not interactive and not args.apply:
        progress = Progress()
        print(bold("\n" + tr("Просмотр: ищу, ничего не трогаю…")))
        plan = night.plan_night(rules, progress)
        progress.clear()
        junk = plan.check.by_mode("delete")
        print("  " + tr("Кэши и временное: {size}", size=human_size(sum(f.size for f in junk))))
        print("  " + tr("Удалю сразу (после сверки): {count}, {size}", count=objects(len(plan.auto)),
                        size=human_size(sum(f.size for f in plan.auto))))
        for f in sorted(plan.auto, key=lambda f: -f.size)[:8]:
            print(dim(f"      {display(f.path)} — {f.reason}"))
        waiting = plan.morning + [f for f, _ in plan.held]
        print("  " + tr("До утра в «{folder}»: {count}, {size}", folder=config.REVIEW_DIR_NAME,
                        count=objects(len(waiting)), size=human_size(sum(f.size for f in waiting))))
        print("  " + tr("Только в отчёт: {size}", size=human_size(sum(f.size for f in plan.check.by_mode("report")))))
        print(cyan(tr("Это был просмотр. Выполнить: filecleaner night --apply")))
        return 0
    if not _licensed(unattended=getattr(args, "yes", False)):
        return 3
    if not getattr(args, "yes", False) and not confirm("\n" + tr("Приступить? Дальше можно уйти — всё сделается само")):
        print(tr("Отменено, ничего не тронуто."))
        return 0

    progress = Progress()
    log, log_path = _night_log(progress)
    out = night.run_night(rules, log, progress)
    progress.clear()
    moved = sum(r.moved for _, _, r in out.sorted)
    print(green("\n" + tr("Готово: освобождено {freed} (кэши {junk}, проверенные копии {copies}), разложено {sorted}.",
                          freed=human_size(out.junk_freed + out.auto_freed), junk=human_size(out.junk_freed),
                          copies=human_size(out.auto_freed), sorted=objects(moved))))
    if out.waiting:
        print(yellow(tr("Ждёт утра: {count}, {size} — «{folder}», потом «Утвердить удаление».",
                        count=objects(out.waiting), size=human_size(out.waiting_bytes), folder=config.REVIEW_DIR_NAME)))
    for note in out.notes[:10]:
        print(dim(f"  {note}"))
    for error in out.errors[:10]:
        print(yellow(f"  ! {error}"))
    print(dim(tr("Отчёт: {path}", path=out.report) + "\n" + tr("Журнал: {path}", path=log_path)))
    night.after(rules.get("night.after", "nothing"), log)
    return 0


# ======================================================================= меню
QUIT = ("0", "q", "й")  # «й» — та же клавиша, что q, в русской раскладке


def menu_items() -> list[tuple]:
    """Пункты меню: (клавиша, название на языке программы, команда)."""
    return [
        ("1", tr("Приступай: чистка всех дисков и сортировка за один раз (можно на ночь)"), cmd_night),
        ("2", tr("Скан: что занимает место"), cmd_scan),
        ("3", tr("Проверка: мусор, дубли и лишнее → «Ready for approval»"), cmd_check),
        ("4", tr("Сортировка: разложить файлы по папкам"), cmd_sort),
        ("5", tr("Утвердить удаление (папка «Ready for approval»)"), cmd_approve),
        ("6", tr("Сжатие: освободить место без удаления"), cmd_compress),
        ("7", tr("История"), cmd_history),
        ("8", tr("Отменить действие"), cmd_undo),
        ("9", tr("Правила: что считать ненужным"), cmd_rules),
    ]


def menu() -> int:
    print(bold(f"File Cleaner {__version__}")
          + dim(" — " + tr("порядок на диске, удаление только с твоего подтверждения")))
    last = night.last_run()
    if last:
        print(dim(tr("Последний «Приступай»: {date} — освобождено {freed}, разложено {sorted}. Отчёт: {report}",
                     date=last["finished"].replace("T", " "), freed=human_size(last["freed"]), sorted=last["sorted"],
                     report=last["report"])))
    waiting = review.find_batches()
    if waiting:
        size = sum(b.size() for b in waiting)
        print(yellow(tr("В «{folder}» ждёт решения {size} — пункт 5.", folder=config.REVIEW_DIR_NAME,
                        size=human_size(size))))
    info = licensing.status()
    if info["state"] == "expired" or (info["state"] == "trial" and info["days_left"] <= 7):
        print(yellow(_license_line(info)))
    items = menu_items()
    while True:
        print()
        for key, title, _ in items:
            print(f"  {key}  {title}")
        print("  0  " + tr("Выход"))
        choice = ask(tr("Пункт: "))
        if choice is None or choice in QUIT:
            return 0
        action = next((fn for key, _, fn in items if key == choice), None)
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
            print(yellow("\n" + tr("Прервано.")))


# ======================================================================= запуск
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="filecleaner",
        description=tr("Скан, проверка, сортировка и удаление ненужного — только с твоего подтверждения. "
                       "Без аргументов открывается меню."),
    )
    parser.add_argument("--version", action="version", version=f"File Cleaner {__version__}")
    parser.add_argument("--rules", help=tr("свой файл правил вместо стандартного"))
    sub = parser.add_subparsers(dest="command", metavar=tr("КОМАНДА"))

    def add(name: str, func, help_text: str) -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=help_text, description=help_text)
        p.set_defaults(func=func)
        return p

    p = add("schedule", cmd_schedule, tr("«Приступай» каждую ночь (Планировщик Windows)"))
    p.add_argument("--at", metavar=tr("ЧЧ:ММ"), help=tr("во сколько запускать, например 03:00"))
    p.add_argument("--no-wake", action="store_true", help=tr("не будить компьютер ради этого"))
    p.add_argument("--off", action="store_true", help=tr("выключить ночной запуск"))
    add("update", cmd_update, tr("проверить, вышла ли новая версия"))
    p = add("license", cmd_license, tr("лицензия: показать или ввести ключ"))
    p.add_argument("key", nargs="*", help=tr("ключ FC1-… из письма"))
    p = add("gui", cmd_gui, tr("окно программы: всё кнопками"))
    p.add_argument("--no-window", action="store_true", help=tr("не открывать окно — только напечатать адрес"))
    p.add_argument("--port", type=int, default=0, help=tr("порт (по умолчанию — любой свободный)"))
    p = add("night", cmd_night, tr("приступай: чистка всех дисков и сортировка за один раз — можно на ночь "
                                   "(без --apply только показать)"))
    p.add_argument("--apply", action="store_true", help=tr("выполнить, а не только показать"))
    p.add_argument("--yes", action="store_true", help=tr("не спрашивать подтверждение"))
    p.add_argument("--after", choices=night.AFTER,
                   help=tr("что сделать в конце (по умолчанию — night.after в правилах)"))
    add("scan", cmd_scan, tr("что занимает место"))
    p = add("check", cmd_check, tr("найти мусор, дубли и лишнее (с --apply: кэши удалить, остальное — на проверку)"))
    p.add_argument("--apply", action="store_true", help=tr("выполнить, а не только показать"))
    p.add_argument("--yes", action="store_true", help=tr("не спрашивать подтверждение"))
    p = add("approve", cmd_approve, tr("утвердить: вернуть отмеченное, удалить остальное из «Ready for approval»"))
    p.add_argument("--yes", action="store_true", help=tr("не спрашивать подтверждение"))
    p = add("sort", cmd_sort, tr("разложить файлы папки по секторам и типам"))
    p.add_argument("folder", nargs="?", help=tr("папка (по умолчанию Загрузки)"))
    p.add_argument("--by-year", action="store_true", help=tr("ещё и по годам"))
    p.add_argument("--apply", action="store_true", help=tr("выполнить, а не только показать"))
    p.add_argument("--yes", action="store_true", help=tr("не спрашивать подтверждение"))
    p = add("compress", cmd_compress, tr("сжать папку прозрачным сжатием Windows"))
    p.add_argument("folder", help=tr("папка"))
    p.add_argument("--apply", action="store_true", help=tr("выполнить, а не только показать"))
    p.add_argument("--yes", action="store_true", help=tr("не спрашивать подтверждение"))
    add("history", cmd_history, tr("что программа уже делала"))
    p = add("undo", cmd_undo, tr("отменить действие (по умолчанию последнее)"))
    p.add_argument("id", nargs="?", help=tr("номер из history или last"))
    p.add_argument("--yes", action="store_true", help=tr("не спрашивать подтверждение"))
    p = add("rules", cmd_rules, tr("показать правила; --edit — открыть их для правки"))
    p.add_argument("--edit", action="store_true", help=tr("открыть файл правил"))
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
    try:  # язык — до первого вывода и до справки argparse; свой файл --rules уточнит его ниже
        language = Rules.load().get("ui.language", "auto")
    except RulesError:
        language = "auto"
    i18n.set_language(i18n.resolve(language))
    args = build_parser().parse_args(argv)
    if args.command is None:
        try:
            return menu()
        except KeyboardInterrupt:
            return 130
    try:
        rules = Rules.load(Path(args.rules) if args.rules else None)
        i18n.set_language(i18n.resolve(rules.get("ui.language", "auto")))
    except RulesError as exc:
        print(red(str(exc)))
        return 2
    try:
        return args.func(args, rules) or 0
    except KeyboardInterrupt:
        print(yellow("\n" + tr("Прервано.")))
        return 130
