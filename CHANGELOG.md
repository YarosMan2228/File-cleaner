# Что нового

## 0.2.1

- **«Проверить содержимое»** в «На решение»: распакованный архив сверяется с папкой файл за файлом
  (zip — сам, rar и 7z — через 7-Zip или встроенный в Windows tar), копия — с оригиналом байт в байт.
  У каждого файла видно «✓ можно удалять» или «✗ не удалять» с причиной; не прошедшее сверку не выбрано.
- Ночью сверяется всё отложенное до утра — утром итог уже написан. Проверенные rar и 7z в Загрузках
  и на Рабочем столе удаляются, как проверенные zip.
- Консоль: `filecleaner verify`.

English:
- **«Check contents»** in «To decide»: an extracted archive is compared with its folder file by file
  (zip by itself, rar and 7z via 7-Zip or the tar built into Windows), a copy — with the original byte for byte.
  Each file shows «✓ safe to delete» or «✗ keep it» with the reason; whatever fails the check is unselected.
- At night everything set aside for the morning is checked, so the result is already there in the morning.
  Verified rar and 7z archives in Downloads and on the Desktop are deleted just like verified zips.
- Console: `filecleaner verify`.

## 0.2.0

- **Каждую ночь сам:** «Приступай» запускается по расписанию (Планировщик Windows), может будить компьютер,
  от батареи не запускается. Настройки → «Приступай» или `filecleaner schedule --at 03:00`.
- **Обновления:** раз в день программа проверяет, вышла ли новая версия, и показывает ссылку на неё.
- **Лицензия:** 30 дней пробного периода, потом нужен ключ (Настройки → «Лицензия»).
- **Консоль на языке программы:** меню и команды — по-русски или по-английски.
- Секторы по умолчанию — общие: «Учёба», «Работа», «Виртуалки» (по-английски — Study, Work, Virtual machines).
- Исправлено: без консоли (двойной щелчок по File Cleaner.exe, запуск из Планировщика) программа закрывалась сразу.

English:
- **Every night by itself:** «Clean up» runs on a schedule (Windows Task Scheduler), can wake the computer,
  never starts on battery. Settings → «Clean up» or `filecleaner schedule --at 03:00`.
- **Updates:** once a day the program checks whether a new version is out and shows a link to it.
- **License:** 30-day trial, then a key is needed (Settings → License).
- **Console in the program's language:** menu and commands in Russian or English.
- Generic default sectors: Study, Work, Virtual machines.
- Fixed: without a console (double-clicking File Cleaner.exe, Task Scheduler) the program closed right away.

## 0.1.0

- Первая сборка: окно с кнопками, «Приступай» — чистка и сортировка за один раз, папка «Ready for approval»,
  локальный ИИ через Ollama, английский интерфейс окна, установщик.
