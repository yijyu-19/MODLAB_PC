# -*- mode: python ; coding: utf-8 -*-
"""
MODLAB PC 封包配置。

运行依赖（打包.bat 会先安装）：
- Pytonium（含 CEF / bin_win）
- requests 及其传递依赖
- 标准库 tkinter（文件选择框）、sqlite3（CrossCore 共享库）
- numpy / pytoshop / tqdm / typing_extensions（碧蓝航线 TachieHelper 立绘导出）
- pypinyin（干员搜索框拼音匹配）
  PySide6 仅 TachieHelper 的 GUI 使用，导出流程不需要，保持排除

界面资源 ui / lang / config / game / image 不打进 _internal：
封包后 get_app_root() 指向 exe 所在目录，由 打包.bat 拷到 exe 旁边。
"""

from __future__ import annotations

from pathlib import Path

import Pytonium
from PyInstaller.utils.hooks import collect_all

from build_cython import CYTHON_MODULES, compiled_pyd_map
from core.app_version import FROZEN_VERSION_FILE, read_app_version
from tools.sanitize_release_gameconfigs import gameconfig_datas, prepare_release_templates

_SPECDIR = Path(SPECPATH).resolve()
_PYTONIUM = Path(Pytonium.__file__).resolve().parent

_VERSION = read_app_version(_SPECDIR)
print(f"[MODLAB_PC.spec] version={_VERSION} (from modlab_PC_main.py)")
_VERSION_STAMP = _SPECDIR / "build" / FROZEN_VERSION_FILE
_VERSION_STAMP.parent.mkdir(parents=True, exist_ok=True)
_VERSION_STAMP.write_text(_VERSION, encoding="utf-8")
# CEF/Pytonium 在含中文的路径下会 GPU 崩溃（error_code=63），exe/目录必须用 ASCII。
# 对外中文名由 build_release.py 用于 zip / 快捷方式。
_APP_NAME = "MODLAB_PC"
_ICON = _SPECDIR / "icon.ico"


def _safe_collect(package_name: str):
    try:
        return collect_all(package_name)
    except Exception:
        return [], [], []


_collected_datas = []
_collected_binaries = []
_collected_hidden = []
for _pkg in (
    "requests",
    "certifi",
    "charset_normalizer",
    "idna",
    "urllib3",
    "UnityPy",
    "lz4",
    "PIL",
    "Pillow",
    "fmod_toolkit",
    # fmod_toolkit 运行时懒加载 pyfmodex，collect_all(fmod_toolkit) 不会带上它
    "pyfmodex",
    "brotli",
    "texture2ddecoder",
    "etcpak",
    "astc_encoder",
    "fsspec",
    "attrs",
    "tpk_ar",
    # UnityPy ArchiveStorageManager 延迟导入（部分加密 AB）
    "Crypto",
    # 碧蓝航线立绘导出（--run-script 在本 exe 内执行 TachieHelper）
    "numpy",
    "pytoshop",
    "tqdm",
    "colorama",
    "typing_extensions",
    # 干员搜索框拼音匹配
    "pypinyin",
):
    _d, _b, _h = _safe_collect(_pkg)
    _collected_datas += _d
    _collected_binaries += _b
    _collected_hidden += _h

_datas = [
    (str(_PYTONIUM / "bin_win"), "Pytonium/bin_win"),
    (str(_VERSION_STAMP), "."),
]
if (_PYTONIUM / "pytonium.pyi").is_file():
    _datas.append((str(_PYTONIUM / "pytonium.pyi"), "Pytonium"))

# brotli 是单文件模块 + 扩展，collect_all 可能漏掉 brotli.py
try:
    import brotli as _brotli_mod

    _brotli_py = Path(getattr(_brotli_mod, "__file__", "") or "")
    if _brotli_py.is_file() and _brotli_py.suffix.lower() == ".py":
        _datas.append((str(_brotli_py), "."))
except Exception:
    pass

prepare_release_templates(_SPECDIR)
_datas.extend(gameconfig_datas(_SPECDIR))

_hiddenimports = list(
    dict.fromkeys(
        [
            "Pytonium",
            "tkinter",
            "tkinter.filedialog",
            "tkinter.ttk",
            "_tkinter",
            "requests",
            "certifi",
            "charset_normalizer",
            "idna",
            "urllib3",
            "core",
            "core.app_state",
            "core.auth",
            "core.announcements",
            "core.app_version",
            "core.arknights_mobile",
            "core.arknights_mod_adapt",
            "core.azurlane",
            "core.character_export",
            "core.config",
            "core.crosscore_db",
            "core.debug_log",
            "core.dependency",
            "core.dialogs",
            "core.dol",
            "core.emulator",
            "core.endfield",
            "core.game_caps",
            "core.minecraft_pcl",
            "core.games",
            "core.i18n",
            "core.install",
            "core.majsoul",
            "core.mod_convert",
            "core.mod_preview",
            "core.mods",
            "core.nizhidaoma",
            "core.path_guard",
            "core.paths",
            "core.pinyin_search",
            "core.cloud_control",
            "core.offline_state",
            "Crypto.Cipher._mode_gcm",
            "core.prefs",
            "core.preview_http",
            "core.purchases",
            "core.py_script",
            "core.self_check",
            "core.settings",
            "core.sponsor_bg",
            "core.theme",
            "core.tls_trust",
            "core.window_util",
            "UnityPy",
            "UnityPy.export",
            "UnityPy.export.AudioClipConverter",
            "UnityPy.helpers",
            "UnityPy.helpers.CompressionHelper",
            "lz4",
            "lz4.block",
            "lz4.frame",
            "PIL",
            "PIL.Image",
            "fmod_toolkit",
            "fmod_toolkit.fmod",
            "fmod_toolkit.importer",
            "pyfmodex",
            "pyfmodex.enums",
            "pyfmodex.flags",
            "pyfmodex.structure_declarations",
            "brotli",
            "_brotli",
            "texture2ddecoder",
            "etcpak",
            "astc_encoder",
            "fsspec",
            "attrs",
            "tpk_ar",
            "Crypto",
            "Crypto.Cipher",
            "Crypto.Cipher.AES",
            "sqlite3",
            "_sqlite3",
            "numpy",
            "pytoshop",
            "pytoshop.enums",
            "pytoshop.user",
            "pytoshop.user.nested_layers",
            "tqdm",
            "colorama",
            "typing_extensions",
            "pypinyin",
        ]
        + _collected_hidden
    )
)

if not _ICON.is_file():
    raise SystemExit(f"缺少图标文件: {_ICON}")

a = Analysis(
    [str(_SPECDIR / "modlab_PC_main.py")],
    pathex=[str(_SPECDIR)],
    binaries=_collected_binaries,
    datas=_datas + _collected_datas,
    hiddenimports=_hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "matplotlib",
        "pandas",
        "PyQt5",
        "PyQt6",
        "PySide2",
        "PySide6",
        "shiboken2",
        "shiboken6",
    ],
    noarchive=False,
)

# 关键模块换成 build_cython.py 编译出的 .pyd：依赖照常按 .py 分析，只替换最终收录的文件
_NATIVE = compiled_pyd_map()
_MISSING_NATIVE = [rel for rel in CYTHON_MODULES if rel[:-3].replace("/", ".") not in _NATIVE]
if _MISSING_NATIVE:
    print(f"[MODLAB_PC.spec] WARNING: not compiled, bundled as plain .py: {_MISSING_NATIVE}")
if _NATIVE:
    a.pure = [entry for entry in a.pure if entry[0] not in _NATIVE]
    a.binaries += [
        ("\\".join(mod.split(".")[:-1] + [pyd.name]), str(pyd), "EXTENSION")
        for mod, pyd in _NATIVE.items()
    ]
    print(f"[MODLAB_PC.spec] native modules: {sorted(_NATIVE)}")

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=_APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(_ICON),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name=_APP_NAME,
)
