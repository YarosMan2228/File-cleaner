"""Пути и справочники: где что лежит, типы файлов, какие папки не трогать никогда."""
from __future__ import annotations

import os
import re
from pathlib import Path

try:
    import winreg
except ImportError:  # не Windows
    winreg = None

HOME = Path.home()
LOCALAPPDATA = Path(os.environ.get("LOCALAPPDATA") or HOME / "AppData" / "Local")
APPDATA = Path(os.environ.get("APPDATA") or HOME / "AppData" / "Roaming")
PROGRAMDATA = Path(os.environ.get("PROGRAMDATA") or r"C:\ProgramData")
TEMP = Path(os.environ.get("TEMP") or LOCALAPPDATA / "Temp")
WINDIR = Path(os.environ.get("WINDIR") or r"C:\Windows")

# Индекс, журнал действий и отчёты программы.
DATA_DIR = Path(os.environ.get("FILECLEANER_HOME") or LOCALAPPDATA / "FileCleaner")

# Кандидаты на удаление переезжают сюда и ждут твоего решения.
REVIEW_DIR_NAME = "Ready for approval"
REVIEW_HOME = Path(os.environ.get("FILECLEANER_REVIEW_DIR") or HOME / REVIEW_DIR_NAME)
RETURN_DIR_NAME = "_ВЕРНУТЬ"


def review_root(path: Path) -> Path:
    """Папка «Ready for approval» на том же диске, что и файл, — перенос туда мгновенный."""
    drive = Path(path).drive.upper()
    if not drive or drive == REVIEW_HOME.drive.upper() or drive.startswith("\\\\"):
        return REVIEW_HOME
    return Path(drive + "\\") / REVIEW_DIR_NAME


# ------------------------------------------------------------------ личные папки
FOLDER_TITLES = {
    "desktop": "Рабочий стол",
    "downloads": "Загрузки",
    "documents": "Документы",
    "pictures": "Изображения",
    "music": "Музыка",
    "videos": "Видео",
}
_SHELL_FOLDERS = {  # имя в реестре, запасной путь
    "desktop": ("Desktop", "Desktop"),
    "downloads": ("{374DE290-123F-4565-9164-39C4925E467B}", "Downloads"),
    "documents": ("Personal", "Documents"),
    "pictures": ("My Pictures", "Pictures"),
    "music": ("My Music", "Music"),
    "videos": ("My Video", "Videos"),
}


def user_folders() -> dict[str, Path]:
    """Настоящие пути личных папок (Изображения, например, могут лежать в OneDrive)."""
    key = None
    if winreg is not None:
        try:
            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders",
            )
        except OSError:
            key = None
    result: dict[str, Path] = {}
    for name, (reg_name, fallback) in _SHELL_FOLDERS.items():
        path = HOME / fallback
        if key is not None:
            try:
                value, _ = winreg.QueryValueEx(key, reg_name)
                path = Path(os.path.expandvars(value))
            except OSError:
                pass
        if path.is_dir():
            result[name] = path
    if key is not None:
        key.Close()
    return result


# ------------------------------------------------------------------ типы файлов
TYPES: dict[str, frozenset[str]] = {
    "Изображения": frozenset({
        "jpg", "jpeg", "jfif", "png", "gif", "bmp", "webp", "heic", "heif", "avif", "tif", "tiff",
        "svg", "ico", "raw", "cr2", "cr3", "nef", "arw", "dng", "psd", "ai", "xcf",
    }),
    "Видео": frozenset({
        "mp4", "mkv", "avi", "mov", "wmv", "flv", "webm", "m4v", "mpg", "mpeg", "3gp", "mts", "m2ts",
    }),
    "Музыка": frozenset({"mp3", "wav", "flac", "aac", "ogg", "m4a", "wma", "opus", "aiff", "mid", "midi"}),
    "Документы": frozenset({"pdf", "doc", "docx", "odt", "rtf", "txt", "md", "tex", "xps", "oxps", "pages"}),
    "Таблицы": frozenset({"xls", "xlsx", "xlsm", "xlsb", "ods", "csv", "tsv", "numbers"}),
    "Презентации": frozenset({"ppt", "pptx", "pps", "ppsx", "odp", "key"}),
    "Книги": frozenset({"epub", "fb2", "mobi", "azw3", "djvu", "djv"}),
    "Архивы": frozenset({"zip", "rar", "7z", "tar", "gz", "tgz", "bz2", "xz", "zst"}),
    "Образы дисков": frozenset({"iso", "img", "vhd", "vhdx", "vmdk", "vdi", "ova", "ovf"}),
    "Установщики": frozenset({"exe", "msi", "msix", "msixbundle", "appx", "appxbundle", "apk", "xapk"}),
    "Код": frozenset({
        "py", "ipynb", "js", "ts", "jsx", "tsx", "java", "kt", "c", "h", "cpp", "hpp", "cc", "cs", "go",
        "rs", "rb", "php", "swift", "dart", "lua", "r", "m", "html", "htm", "css", "scss", "json",
        "xml", "yaml", "yml", "toml", "ini", "cfg", "sql", "ddl", "sh", "bat", "cmd", "ps1", "jar",
    }),
    "Базы данных": frozenset({"db", "sqlite", "sqlite3", "mdb", "accdb", "dmd"}),
    "3D-модели": frozenset({"blend", "fbx", "obj", "stl", "3mf", "gltf", "glb", "dae", "3ds", "max", "step", "stp"}),
    "Шрифты": frozenset({"ttf", "otf", "woff", "woff2", "fon"}),
    "Торренты": frozenset({"torrent"}),
}
EXT_TO_TYPE = {ext: name for name, exts in TYPES.items() for ext in exts}
OTHER_TYPE = "Прочее"

# Подпапки «по смыслу» внутри типа: (тип, подпапка, шаблон имени).
SUBTYPE_RULES: list[tuple[str, str, re.Pattern[str]]] = [
    ("Изображения", "Скриншоты", re.compile(
        r"^(screenshot|screen shot|снимок экрана|знімок екрана|скриншот|скріншот|ekrānuzņēmums)", re.I)),
    ("Изображения", "Фото", re.compile(r"^((img|dsc|dscn|dscf|pxl|photo)[_-]?\d{4,}|\d{8}_\d{6})", re.I)),
    ("Видео", "Записи экрана", re.compile(
        r"^(screen recording|запись экрана|запис екрана|\d{4}-\d{2}-\d{2} \d{2}-\d{2}-\d{2})", re.I)),
    ("Видео", "С телефона", re.compile(r"^((vid|pxl)[_-]?\d{4,}|\d{8}_\d{6})", re.I)),
]

ARCHIVE_EXTS = TYPES["Архивы"]
INSTALLER_EXTS = frozenset({"exe", "msi", "msix", "msixbundle", "appx", "appxbundle"})
PARTIAL_EXTS = frozenset({"crdownload", "part", "partial", "opdownload", "download", "!ut", "!qb"})
VM_DISK_EXTS = frozenset({"vdi", "vmdk", "vhd", "vhdx"})
SHORTCUT_EXTS = frozenset({"lnk", "url"})
SERVICE_NAMES = frozenset({"desktop.ini", "thumbs.db", "ehthumbs.db", ".ds_store"})

# Уже сжатые форматы: прозрачное сжатие NTFS им почти ничего не даёт.
INCOMPRESSIBLE_EXTS = frozenset({
    "zip", "rar", "7z", "gz", "tgz", "bz2", "xz", "zst", "cab", "jar", "apk", "xapk", "msix", "appx",
    "jpg", "jpeg", "jfif", "png", "gif", "webp", "heic", "heif", "avif",
    "mp4", "mkv", "avi", "mov", "wmv", "flv", "webm", "m4v", "mpg", "mpeg", "3gp", "mts", "m2ts",
    "mp3", "aac", "ogg", "m4a", "wma", "opus", "flac",
    "docx", "xlsx", "pptx", "odt", "ods", "odp", "epub", "pdf", "iso",
})

# ------------------------------------------------------------------ что не трогать
# В эти папки программа не заходит вообще.
SKIP_DIR_NAMES = frozenset({
    REVIEW_DIR_NAME.lower(), "$recycle.bin", "system volume information", "appdata", "windows",
    "program files", "program files (x86)", "programdata", "windowsapps", "$winreagent", "recovery",
    "perflogs", "config.msi", "msocache", "node_modules", "__pycache__", "site-packages", "venv",
    "steamapps", "steamlibrary", "epic games", "riot games", "xboxgames", "my games", "saved games",
    "cacheclip",  # кэш DaVinci Resolve: одинаковые кадры — не копии, программа ведёт его сама
})
# Папка проекта: файлы внутри по одному не удаляем и не переносим.
PROJECT_MARKERS = frozenset({
    ".git", ".hg", ".svn", "package.json", "pyproject.toml", "setup.py", "requirements.txt",
    "pyvenv.cfg", "pom.xml", "build.gradle", "build.gradle.kts", "settings.gradle", "cargo.toml",
    "go.mod", "cmakelists.txt", "composer.json", "gemfile", "projectsettings", ".idea",
})
# Папка программы (есть .dll) или решение Visual Studio: тоже целиком или никак.
PROTECTED_SUFFIXES = (".sln", ".csproj", ".vcxproj", ".uproject", ".dll", ".sys")

# ------------------------------------------------------------------ системный мусор
TEMP_DIRS = [TEMP, WINDIR / "Temp", LOCALAPPDATA / "Microsoft" / "Windows" / "INetCache"]
CRASH_DIRS = [
    LOCALAPPDATA / "CrashDumps",
    LOCALAPPDATA / "Microsoft" / "Windows" / "WER",
    PROGRAMDATA / "Microsoft" / "Windows" / "WER",
]
# (название, процесс, папка User Data в стиле Chromium, дополнительные папки кэша)
BROWSERS: list[tuple[str, str, Path, list[Path]]] = [
    ("Google Chrome", "chrome.exe", LOCALAPPDATA / "Google" / "Chrome" / "User Data", []),
    ("Microsoft Edge", "msedge.exe", LOCALAPPDATA / "Microsoft" / "Edge" / "User Data", []),
    ("Brave", "brave.exe", LOCALAPPDATA / "BraveSoftware" / "Brave-Browser" / "User Data", []),
    ("Vivaldi", "vivaldi.exe", LOCALAPPDATA / "Vivaldi" / "User Data", []),
    ("Яндекс Браузер", "browser.exe", LOCALAPPDATA / "Yandex" / "YandexBrowser" / "User Data", []),
    ("Opera", "opera.exe", APPDATA / "Opera Software" / "Opera Stable",
     [LOCALAPPDATA / "Opera Software" / "Opera Stable" / "Cache"]),
    ("Opera GX", "opera.exe", APPDATA / "Opera Software" / "Opera GX Stable",
     [LOCALAPPDATA / "Opera Software" / "Opera GX Stable" / "Cache"]),
]
CHROMIUM_PROFILE_CACHES = ("Cache", "Code Cache", "GPUCache", "DawnCache", "DawnWebGPUCache", "DawnGraphiteCache")
CHROMIUM_SHARED_CACHES = ("GrShaderCache", "ShaderCache", "GraphiteDawnCache")
FIREFOX_PROFILES = LOCALAPPDATA / "Mozilla" / "Firefox" / "Profiles"

APP_CACHES: list[tuple[str, str, list[Path]]] = [
    ("Discord", "discord.exe", [APPDATA / "discord" / d for d in ("Cache", "Code Cache", "GPUCache")]),
    ("VS Code", "code.exe", [APPDATA / "Code" / d for d in
                             ("Cache", "CachedData", "Code Cache", "GPUCache", "CachedExtensionVSIXs")]),
    ("Cursor", "cursor.exe", [APPDATA / "Cursor" / d for d in ("Cache", "CachedData", "Code Cache", "GPUCache")]),
    ("Slack", "slack.exe", [APPDATA / "Slack" / d for d in ("Cache", "Code Cache", "GPUCache")]),
    ("Steam", "steam.exe", [LOCALAPPDATA / "Steam" / "htmlcache"]),
]
DEV_CACHES: list[tuple[str, Path]] = [
    ("pip", LOCALAPPDATA / "pip" / "cache"),
    ("npm", LOCALAPPDATA / "npm-cache" / "_cacache"),
    ("Yarn", LOCALAPPDATA / "Yarn" / "Cache"),
    ("Bun", HOME / ".bun" / "install" / "cache"),
    ("uv", LOCALAPPDATA / "uv" / "cache"),
    ("NuGet", LOCALAPPDATA / "NuGet" / "v3-cache"),
    ("Go", LOCALAPPDATA / "go-build"),
]
SHADER_CACHES = [
    LOCALAPPDATA / "D3DSCache",
    LOCALAPPDATA / "NVIDIA" / "DXCache",
    LOCALAPPDATA / "NVIDIA" / "GLCache",
    HOME / "AppData" / "LocalLow" / "NVIDIA" / "PerDriverVersion" / "DXCache",
    LOCALAPPDATA / "AMD" / "DxCache",
    LOCALAPPDATA / "AMD" / "DxcCache",
    LOCALAPPDATA / "AMD" / "VkCache",
]
