# -*- coding: utf-8 -*-
"""Rebuild only the release zip from existing dist/MODLAB_PC (no PyInstaller)."""

from __future__ import annotations

import shutil
import subprocess
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist" / "MODLAB_PC"


def main() -> int:
    from build_release import sanitize_dist_machine_paths
    from tools.sanitize_release_gameconfigs import gameconfig_datas, prepare_release_templates

    prepare_release_templates(ROOT)
    if not DIST.is_dir():
        raise SystemExit(f"Missing dist folder: {DIST}")
    if (DIST / "game").is_dir():
        prepare_release_templates(DIST)
        sanitize_dist_machine_paths(DIST)
    packaged = gameconfig_datas(DIST if (DIST / "game").is_dir() else ROOT)
    print(f"templates={len(packaged)}", flush=True)

    from core.app_version import display_release_name, main_py_path, read_app_version
    version = read_app_version(ROOT)
    display = display_release_name(version)
    print(f"version_source={main_py_path(ROOT)}", flush=True)
    print(f"version={version}", flush=True)

    gd = DIST / "game dependency"
    if gd.exists():
        shutil.rmtree(gd, ignore_errors=True)
        print("removed empty game dependency")

    exe = DIST / "MODLAB_PC.exe"
    shortcut = DIST / f"{display}.lnk"
    for old in DIST.glob("*.lnk"):
        if old != shortcut:
            old.unlink(missing_ok=True)
            print("removed", old.name)

    ps = f"""
$w = New-Object -ComObject WScript.Shell
$s = $w.CreateShortcut('{str(shortcut).replace("'", "''")}')
$s.TargetPath = '{str(exe).replace("'", "''")}'
$s.WorkingDirectory = '{str(DIST).replace("'", "''")}'
$s.IconLocation = '{str(exe).replace("'", "''")}'
$s.Description = '{display.replace("'", "''")}'
$s.Save()
"""
    subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
        check=False,
    )

    zip_path = ROOT / "dist" / f"{display}.zip"
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in DIST.rglob("*"):
            if path.is_file():
                zf.write(path, arcname=str(path.relative_to(DIST)))
    print(f"zip={zip_path}")
    print(f"size={zip_path.stat().st_size}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
