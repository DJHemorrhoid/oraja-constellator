from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from datetime import datetime
from pathlib import Path

PRESERVED_DIRS = {"data", "logs", "filter_packs", "analysis_packs"}


def wait_for_process(pid: int, timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            return
        time.sleep(0.25)


def find_payload_root(extract_dir: Path) -> Path:
    direct = extract_dir / "start_oraja_constellator.bat"
    if direct.is_file():
        return extract_dir
    candidates = [p.parent for p in extract_dir.rglob("start_oraja_constellator.bat")]
    candidates = [p for p in candidates if (p / "python" / "launch_app.py").is_file()]
    if len(candidates) != 1:
        raise RuntimeError("更新ZIPの構成を判定できません")
    return candidates[0]


def copy_tree_replace(source: Path, target: Path) -> None:
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(source, target)


def apply_update(root: Path, zip_path: Path) -> None:
    backup_root = root / "data" / "update_backups" / datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="oraja_constellator_update_") as td:
        extract_dir = Path(td)
        with zipfile.ZipFile(zip_path) as archive:
            base = extract_dir.resolve()
            for member in archive.infolist():
                destination = (extract_dir / member.filename).resolve()
                if not destination.is_relative_to(base):
                    raise RuntimeError("更新ZIPに不正なパスが含まれています")
            archive.extractall(extract_dir)
        payload = find_payload_root(extract_dir)
        if not (payload / "python" / "launch_app.py").is_file():
            raise RuntimeError("更新ZIPに起動ファイルがありません")

        for name in ("start_oraja_constellator.bat", "README.txt"):
            current = root / name
            if current.exists():
                shutil.copy2(current, backup_root / name)
        for name in ("python", "docs"):
            current = root / name
            if current.exists():
                shutil.copytree(current, backup_root / name)

        for name in ("python", "docs"):
            source = payload / name
            if source.exists():
                copy_tree_replace(source, root / name)
        for name in ("start_oraja_constellator.bat", "README.txt"):
            source = payload / name
            if source.is_file():
                shutil.copy2(source, root / name)

        # updateフォルダーは補助資料だけ更新し、公開先設定は保持する。
        source_update = payload / "update"
        target_update = root / "update"
        if source_update.exists():
            target_update.mkdir(parents=True, exist_ok=True)
            for item in source_update.iterdir():
                if item.name == "update_source.json":
                    continue
                target = target_update / item.name
                if item.is_dir():
                    copy_tree_replace(item, target)
                else:
                    shutil.copy2(item, target)

        # 古い直下Pythonファイルなどを整理する。利用者データは触らない。
        obsolete = [
            "launch_app.py", "startup_check.py", "oraja_constellator.py", "bms_filter_folder_tool.py", "core.py",
            "attr_core.py", "attribute_rule_pack.py", "requirements.txt", "diagnose_startup.bat",
            "CHANGELOG.txt",
        ]
        for name in obsolete:
            path = root / name
            if path.is_file():
                path.unlink()
        (root / "start_bms_filter_folder_tool.bat").unlink(missing_ok=True)
        for notes in root.glob("RELEASE_NOTES_v*.txt"):
            notes.unlink(missing_ok=True)


def restart_tool(root: Path) -> None:
    bat = root / "start_oraja_constellator.bat"
    if os.name == "nt" and bat.is_file():
        subprocess.Popen(["cmd.exe", "/c", "start", "", str(bat)], cwd=root)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--zip", required=True)
    parser.add_argument("--pid", required=True, type=int)
    args = parser.parse_args()
    root = Path(args.root).resolve()
    zip_path = Path(args.zip).resolve()
    try:
        wait_for_process(args.pid)
        apply_update(root, zip_path)
        restart_tool(root)
        return 0
    except Exception as exc:
        log_dir = root / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / "update_error.log").write_text(str(exc), encoding="utf-8")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
