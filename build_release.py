# -*- coding: utf-8 -*-
"""Windows release packager for MODLAB PC.

Reads version from modlab_PC_main.py, runs PyInstaller via MODLAB_PC.spec,
copies runtime assets beside the exe, and builds release zips.

完整打包：收录 game 下全部游戏；不打追加包。
角色预览资源（头像/立绘/spine 等）不打进包，仅保留空目录，由用户在软件内下载。
设备备份（backupfiles / crosscore 镜像）属于本机数据，不打进包。
Platform: Windows x64 only.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

from core.mod_names import LEGACY_DEPENDENCY_DIR, MODERN_DEPENDENCY_DIR

ROOT = Path(__file__).resolve().parent
APP_NAME = "MODLAB_PC"
SPEC_FILE = ROOT / "MODLAB_PC.spec"
ICON = ROOT / "icon.ico"
DIST_DIR = ROOT / "dist" / APP_NAME
BUILD_DIR = ROOT / "build"
DIST_ROOT = ROOT / "dist"

SKIP_DIR_NAMES = {
    "__pycache__",
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "cache",
    ".mypy_cache",
    ".pytest_cache",
}
SKIP_FILE_SUFFIXES = {".pyc", ".pyo", ".log"}

# 角色预览资源：删除文件但保留空文件夹
PREVIEW_ASSET_DIRS = (
    Path("game") / "arknights" / "characters" / "heads",
    Path("game") / "majsoul" / "characters" / "heads",
    Path("game") / "majsoul" / "characters" / "portraits",
    Path("game") / "majsoul" / "characters" / "spines",
    Path("game") / "azurlane" / "characters" / "heads",
)
# 任意游戏 characters/ 下的这些子目录同样清空（新游戏无需逐个登记）
CHARACTER_ASSET_SUBDIRS = ("heads", "portraits", "spines", "AssetBundles")
# 任意游戏下的本机设备数据：清空但保留目录
DEVICE_DATA_SUBDIRS = ("backupfiles",)

# 可再生成的大缓存 / 会话文件：直接删掉
POST_CLEAN_FILES = [
    Path("config") / "oauth_session.json",
    Path("config") / "local_prefs.json",
    Path("config") / "purchases_cache.json",
    Path("config") / "announcements_cache.json",
    Path("config") / "sponsor_bg",
    Path("config") / "offline_state.dat",
    Path("cache"),
    Path("game") / "arknights" / "characters" / "character_table_cache.json",
    Path("game") / "minecraft" / "cache",
    Path("game") / "arknights" / "mods" / "v1.3[全面娘化][机械师]乳摇爆乳超大杯(PC)",
]
for _dep_name in (MODERN_DEPENDENCY_DIR, LEGACY_DEPENDENCY_DIR):
    POST_CLEAN_FILES.extend(
        [
            Path("game") / "arknights" / _dep_name / "M4A" / "mods" / "logs",
            Path("game") / "arknights" / _dep_name / "M4A" / "mods" / "dump",
            Path("game") / "majsoul" / _dep_name / "MajsoulMax" / ".venv",
        ]
    )

# tools/ 含内置 platform-tools（adb），便于无 MuMu / 无系统 ADB 的环境使用
COPY_ROOT_DIRS = ["ui", "lang", "config", "image", "tools"]
# 运行时会被提示阅读的单个文档
COPY_ROOT_FILES = [Path("docs") / "雀魂代理分流说明.txt"]


def run(cmd: list[str]) -> None:
    print("+", " ".join(cmd), flush=True)
    completed = subprocess.run(cmd, cwd=str(ROOT))
    if completed.returncode != 0:
        raise SystemExit(completed.returncode)


def copy_tree(src: Path, dst: Path) -> None:
    """复制目录树；跳过 SKIP_DIR_NAMES / 无用后缀。"""
    if not src.exists():
        dst.mkdir(parents=True, exist_ok=True)
        return
    for path in src.rglob("*"):
        rel = path.relative_to(src)
        if any(part in SKIP_DIR_NAMES for part in rel.parts):
            continue
        if path.is_dir():
            continue
        if path.suffix.lower() in SKIP_FILE_SUFFIXES:
            continue
        target = dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)


def remove_path(path: Path) -> None:
    if not path.exists() and not path.is_symlink():
        return
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path, ignore_errors=True)
    else:
        try:
            path.unlink()
        except OSError:
            pass


def empty_dir(path: Path) -> None:
    """删光目录内容后保留空文件夹。"""
    if path.is_file() or path.is_symlink():
        remove_path(path)
    if path.is_dir():
        for child in list(path.iterdir()):
            remove_path(child)
    path.mkdir(parents=True, exist_ok=True)


def create_shortcut(exe: Path, link_path: Path, description: str) -> None:
    ps = f"""
$w = New-Object -ComObject WScript.Shell
$s = $w.CreateShortcut('{str(link_path).replace("'", "''")}')
$s.TargetPath = '{str(exe).replace("'", "''")}'
$s.WorkingDirectory = '{str(exe.parent).replace("'", "''")}'
$s.IconLocation = '{str(exe).replace("'", "''")}'
$s.Description = '{description.replace("'", "''")}'
$s.Save()
"""
    subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
        check=False,
    )


def zip_dir(src: Path, zip_path: Path, *, arc_root: Path | None = None) -> None:
    """打包目录。arc_root 指定 zip 内相对路径的根（默认 src）。"""
    if zip_path.exists():
        zip_path.unlink()
    base = arc_root if arc_root is not None else src
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in src.rglob("*"):
            if not path.is_file():
                continue
            if any(part in SKIP_DIR_NAMES for part in path.relative_to(src).parts):
                continue
            zf.write(path, arcname=str(path.relative_to(base)))


def copy_all_games(dst_game_root: Path) -> None:
    """完整打包：复制 game 下全部游戏子目录。"""
    src_root = ROOT / "game"
    dst_game_root.mkdir(parents=True, exist_ok=True)
    if not src_root.is_dir():
        return
    for folder in sorted(src_root.iterdir(), key=lambda p: p.name.lower()):
        if not folder.is_dir() or folder.name.startswith("."):
            continue
        print(f"  copy game: {folder.name}", flush=True)
        dst = dst_game_root / folder.name
        remove_path(dst)
        copy_tree(folder, dst)


def sanitize_dist_preview_and_cache(dist_dir: Path) -> None:
    """清掉预览位图/大缓存，预览目录保留为空文件夹。"""
    for rel in POST_CLEAN_FILES:
        remove_path(dist_dir / rel)
    for rel in PREVIEW_ASSET_DIRS:
        empty_dir(dist_dir / rel)
    game_root = dist_dir / "game"
    if game_root.is_dir():
        for game in game_root.iterdir():
            if not game.is_dir():
                continue
            remove_path(game / "crosscore")
            for name in DEVICE_DATA_SUBDIRS:
                if (game / name).exists():
                    empty_dir(game / name)
            chars = game / "characters"
            for name in CHARACTER_ASSET_SUBDIRS:
                if (chars / name).exists():
                    empty_dir(chars / name)
            # 终末地：确保 MOD 库目录存在（占位文件保证空目录能打进 zip）
            if game.name.lower() == "endfield":
                empty_dir(game / "mods")
                (game / "mods" / ".gitkeep").write_text("", encoding="utf-8")


_ABS_PATH_VALUE = re.compile(r"^(\s*[\w.]+\s*=\s*)(?:[A-Za-z]:[\\/]|\\\\).*$")


def sanitize_dist_machine_paths(dist_dir: Path) -> None:
    """清空配置里打包机的绝对路径（如 C:\\Users\\<打包者>\\…），由用户电脑上的 MODLAB 重新写入。"""
    game_root = dist_dir / "game"
    if not game_root.is_dir():
        return
    targets = list(game_root.glob("*/gameconfig.ini"))
    for dep_name in (MODERN_DEPENDENCY_DIR, LEGACY_DEPENDENCY_DIR):
        targets.extend(game_root.glob(f"*/{dep_name}/*/config.ini"))
    for path in targets:
        try:
            raw = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        lines = raw.splitlines()
        cleaned = [_ABS_PATH_VALUE.sub(r"\1", line).rstrip() if _ABS_PATH_VALUE.match(line) else line for line in lines]
        if cleaned != lines:
            ending = "\n" if raw.endswith("\n") else ""
            path.write_text("\n".join(cleaned) + ending, encoding="utf-8")
            print(f"  cleared machine paths: {path.relative_to(dist_dir)}", flush=True)


def main() -> int:
    from tools.sanitize_release_gameconfigs import gameconfig_datas, prepare_release_templates

    templates = prepare_release_templates(ROOT)
    packaged = gameconfig_datas(ROOT)
    print(f"release templates: {len(templates)} gameconfig.ini, spec datas {len(packaged)}", flush=True)

    if sys.platform != "win32":
        print("This release packager only supports Windows.", flush=True)
        return 1

    from core.app_version import display_release_name, main_py_path, read_app_version
    if not ICON.is_file():
        print(f"Missing icon: {ICON}", flush=True)
        return 1
    if not SPEC_FILE.is_file():
        print(f"Missing spec: {SPEC_FILE}", flush=True)
        return 1

    version = read_app_version(ROOT)
    display_name = display_release_name(version)
    zip_name = f"{display_name}.zip"
    print(f"version_source={main_py_path(ROOT)}", flush=True)
    print(f"version={version}", flush=True)
    print(f"display_name={display_name}", flush=True)
    print("pack_mode=full (all games, no addon zips)", flush=True)

    print("[1/5] Install runtime deps + PyInstaller...", flush=True)
    run([sys.executable, "-m", "pip", "install", "-U", "pip"])
    run([sys.executable, "-m", "pip", "install", "-r", str(ROOT / "requirements.txt")])
    run([sys.executable, "-m", "pip", "install", "-U", "pyinstaller"])

    print("[2/5] Clean old build/dist...", flush=True)
    remove_path(BUILD_DIR)
    remove_path(DIST_ROOT)

    print("[3/5] Cython key modules + PyInstaller build...", flush=True)
    run([sys.executable, str(ROOT / "build_cython.py")])
    run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", str(SPEC_FILE)])
    exe = DIST_DIR / f"{APP_NAME}.exe"
    if not exe.is_file():
        print(f"Missing exe: {exe}", flush=True)
        return 1

    print("[4/5] Copy runtime assets (all games)...", flush=True)
    for name in COPY_ROOT_DIRS:
        copy_tree(ROOT / name, DIST_DIR / name)
    for rel in COPY_ROOT_FILES:
        if (ROOT / rel).is_file():
            (DIST_DIR / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / rel, DIST_DIR / rel)
    copy_all_games(DIST_DIR / "game")
    sanitize_dist_preview_and_cache(DIST_DIR)
    sanitize_dist_machine_paths(DIST_DIR)

    shortcut = DIST_DIR / f"{display_name}.lnk"
    create_shortcut(exe, shortcut, display_name)
    if not shortcut.is_file():
        print("Warning: shortcut creation failed; run MODLAB_PC.exe directly.", flush=True)

    print("[5/5] Build release zip...", flush=True)
    zip_path = DIST_ROOT / zip_name
    zip_dir(DIST_DIR, zip_path)

    print("", flush=True)
    print("Done.", flush=True)
    print(f"  dir : {DIST_DIR}", flush=True)
    print(f"  exe : {exe}", flush=True)
    print(f"  lnk : {shortcut}", flush=True)
    print(f"  zip : {zip_path}", flush=True)
    print("  note: keep folder name MODLAB_PC (CEF ASCII path).", flush=True)
    print("  platform: Windows x64 only.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
