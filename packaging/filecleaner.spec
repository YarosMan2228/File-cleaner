# -*- mode: python ; coding: utf-8 -*-
# Сборка File Cleaner: одна папка, две программы — «File Cleaner.exe» (окно) и «filecleaner.exe» (команды).
# Запускать через packaging/build.py — он же создаёт version_info.txt и иконку.
from pathlib import Path

ROOT = Path(SPECPATH).parent
PACK = ROOT / "packaging"
ICON = str(PACK / "icon.ico")
VERSION = str(PACK / "version_info.txt")

datas = [
    (str(ROOT / "filecleaner" / "default_rules.toml"), "filecleaner"),
    (str(ROOT / "filecleaner" / "gui" / "static"), "filecleaner/gui/static"),
]
options = dict(
    pathex=[str(ROOT)],
    hiddenimports=["filecleaner.gui.app"],
    excludes=["tkinter", "unittest", "pydoc", "test", "lib2to3",
              "PIL", "cryptography", "cffi"],  # pypdf читает текст и без них: картинки и AES-PDF не нужны
)

gui = Analysis([str(PACK / "launcher_gui.py")], datas=datas, **options)
cli = Analysis([str(PACK / "launcher_cli.py")], **options)

gui_exe = EXE(
    PYZ(gui.pure), gui.scripts, [], exclude_binaries=True, name="File Cleaner",
    console=False, icon=ICON, version=VERSION, upx=False,
)
cli_exe = EXE(
    PYZ(cli.pure), cli.scripts, [], exclude_binaries=True, name="filecleaner",
    console=True, icon=ICON, version=VERSION, upx=False,
)
COLLECT(gui_exe, cli_exe, gui.binaries, gui.datas, cli.binaries, cli.datas, name="FileCleaner", upx=False)
