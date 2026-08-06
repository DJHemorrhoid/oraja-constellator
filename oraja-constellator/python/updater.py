from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import urllib.request
from pathlib import Path
from typing import Any

UPDATE_SCHEMA_VERSION = 1
DEFAULT_TIMEOUT = 15


class UpdateError(RuntimeError):
    pass


def version_key(value: str) -> tuple[int, ...]:
    parts = re.findall(r"\d+", str(value or ""))
    return tuple(int(part) for part in parts) or (0,)


def is_newer(remote: str, current: str) -> bool:
    return version_key(remote) > version_key(current)


def load_update_source(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"manifest_url": "", "repository_url": "", "timeout_seconds": DEFAULT_TIMEOUT}
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        raise UpdateError(f"更新先設定を読み込めません: {exc}") from exc
    if not isinstance(raw, dict):
        raise UpdateError("更新先設定の形式が正しくありません")
    return {
        "manifest_url": str(raw.get("manifest_url") or "").strip(),
        "repository_url": str(raw.get("repository_url") or "").strip(),
        "timeout_seconds": max(3, min(60, int(raw.get("timeout_seconds") or DEFAULT_TIMEOUT))),
    }


def _request_bytes(url: str, *, timeout: int, user_agent: str) -> bytes:
    if not str(url or "").strip():
        raise UpdateError("ダウンロード先URLが設定されていません")
    request = urllib.request.Request(
        str(url).strip(),
        headers={"User-Agent": user_agent, "Accept": "application/json, application/octet-stream;q=0.9, */*;q=0.8"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()
    except Exception as exc:
        raise UpdateError(f"更新情報を取得できません: {exc}") from exc


def fetch_manifest(url: str, *, timeout: int, user_agent: str) -> dict[str, Any]:
    payload = _request_bytes(url, timeout=timeout, user_agent=user_agent)
    try:
        raw = json.loads(payload.decode("utf-8-sig"))
    except Exception as exc:
        raise UpdateError(f"更新情報のJSONを読み込めません: {exc}") from exc
    if not isinstance(raw, dict):
        raise UpdateError("更新情報の形式が正しくありません")
    schema = int(raw.get("schema_version") or 0)
    if schema != UPDATE_SCHEMA_VERSION:
        raise UpdateError(f"未対応の更新情報形式です: {schema}")
    for key in ("app", "filter_settings", "chart_tendency"):
        value = raw.get(key)
        if value is not None and not isinstance(value, dict):
            raise UpdateError(f"更新情報の{key}が正しくありません")
    return raw


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def verify_sha256(payload: bytes, expected: str) -> None:
    expected = str(expected or "").strip().lower()
    if not expected:
        raise UpdateError("更新ファイルのSHA-256が設定されていません")
    actual = sha256_bytes(payload)
    if actual != expected:
        raise UpdateError(f"更新ファイルのSHA-256が一致しません\n期待値: {expected}\n実測値: {actual}")


def download_verified(entry: dict[str, Any], destination: Path, *, timeout: int, user_agent: str) -> Path:
    url = str(entry.get("url") or "").strip()
    payload = _request_bytes(url, timeout=timeout, user_agent=user_agent)
    verify_sha256(payload, str(entry.get("sha256") or ""))
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".tmp")
    temporary.write_bytes(payload)
    os.replace(temporary, destination)
    return destination


def atomic_replace_with_backup(source: Path, target: Path, backup_dir: Path) -> Path | None:
    if not source.is_file():
        raise UpdateError(f"更新元ファイルが見つかりません: {source}")
    backup: Path | None = None
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        backup_dir.mkdir(parents=True, exist_ok=True)
        import datetime

        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        backup = backup_dir / f"{target.stem}_{stamp}{target.suffix}"
        shutil.copy2(target, backup)
    fd, temp_name = tempfile.mkstemp(prefix=target.name + ".", suffix=".tmp", dir=target.parent)
    os.close(fd)
    temp_path = Path(temp_name)
    try:
        shutil.copy2(source, temp_path)
        os.replace(temp_path, target)
    finally:
        temp_path.unlink(missing_ok=True)
    return backup


def manifest_update_summary(
    manifest: dict[str, Any],
    *,
    app_version: str,
    filter_version: str,
    tendency_version: str,
) -> dict[str, Any]:
    app = manifest.get("app") if isinstance(manifest.get("app"), dict) else {}
    filters = manifest.get("filter_settings") if isinstance(manifest.get("filter_settings"), dict) else {}
    tendency = manifest.get("chart_tendency") if isinstance(manifest.get("chart_tendency"), dict) else {}
    app_remote = str(app.get("version") or "")
    filter_remote = str(filters.get("version") or "")
    tendency_remote = str(tendency.get("version") or "")
    return {
        "app_available": bool(app_remote and is_newer(app_remote, app_version)),
        "filter_available": bool(filter_remote and is_newer(filter_remote, filter_version)),
        "tendency_available": bool(tendency_remote and is_newer(tendency_remote, tendency_version)),
        "app_current": app_version,
        "app_remote": app_remote,
        "filter_current": filter_version,
        "filter_remote": filter_remote,
        "tendency_current": tendency_version,
        "tendency_remote": tendency_remote,
    }
