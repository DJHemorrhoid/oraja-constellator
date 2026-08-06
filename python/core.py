from __future__ import annotations

import bisect
import copy
import gzip
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import unicodedata
import tempfile
import time
import urllib.request
import uuid
from fractions import Fraction
from math import ceil
from statistics import median
from dataclasses import dataclass, field as dc_field, asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import urljoin

APP_NAME = "oraja-constellator"
APP_VERSION = "1.0.17"

from attr_core import (  # noqa: E402  譜面属性解析 v0.9.5
    ATTR_CODES,
    ATTR_CODE_UNDECIDED,
    ATTR_INT_COLUMNS,
    ATTR_LABELS,
    ATTR_NUMERIC_COLUMNS,
    ATTR_VALUE_CHOICES,
    SUBCAT_CODES,
    SUBCAT_NONE,
    SUBCAT_VALUE_CHOICES,
    analyze_attr_file,
    classify_attr,
    classify_subcategory,
    classify_practice_priority,
    migrate_legacy_attr,
    read_attr_overrides,
    set_attr_override,
)
from attribute_rule_pack import get_active_rule_pack_info

MANAGED_ROOT_NAME = "[BMS Filter] 選曲支援"
SETTINGS_VERSION = 23
DEFAULT_SIMPLE_FILTER_SET_ORDER = ["譜面傾向", "平均密度", "プレイ状況", "クリア状況", "スコア状況"]
LEGACY_SIMPLE_FILTER_SET_ORDER = ["プレイ状況", "クリア状況", "スコア状況", "平均密度", "譜面傾向"]
DIFFICULTY_REGISTRY_FILENAME = "difficulty_scale_registry.json"
INSTANT_DENSITY_PROFILE_FILENAME = "instant_density_rank_profiles.json"
INSTANT_TABLE_ID = "__bmscf_instant_density__"
MAKER_TABLE_ID = "__bmscf_maker__"
MATERIAL_ANALYSIS_VERSION = 1
MAKER_METRIC_VERSION = 2
MAKER_ADVANCED_AXIS_MIN = 18.0
INSANE_DIFFICULTY = 5
HIGH_DIFFICULTY_MIN = 5
AUDIO_EXTENSIONS = {".wav", ".ogg", ".mp3", ".flac", ".opus"}
MATERIAL_MAKER_FIELDS = {
    "maker_material_ok",
    "maker_needs_review",
    "maker_unused_audio_count",
}

CLEAR_TYPES = {
    "NO PLAY": 0,
    "FAILED": 1,
    "ASSIST EASY": 2,
    "LIGHT ASSIST EASY": 3,
    "EASY": 4,
    "NORMAL": 5,
    "HARD": 6,
    "EX HARD": 7,
    "FULL COMBO": 8,
    "PERFECT": 9,
    "MAX": 10,
}

FILTER_CATEGORIES: list[tuple[str, str]] = [
    ("record", "プレイレコード"),
    ("analysis", "譜面解析"),
    ("song", "譜面データベース"),
    ("text", "曲情報"),
]

RANK_VALUES = {
    "NO PLAY": -1,
    "F": 0,
    "E": 1,
    "D": 2,
    "C": 3,
    "B": 4,
    "A": 5,
    "AA": 6,
    "AAA": 7,
    "MAX": 8,
}

DENSITY_RANK_VALUES = ["下位10%", "下位25%", "中央50%", "上位25%", "上位10%"]
RHYTHM_VALUES = ["16分系", "12分系"]
ANALYSIS_VERSION = 5  # v0.9.7: 調波グリッド補正・低難度ルール・練習優先度低

FIELD_DEFS: dict[str, dict[str, Any]] = {
    "played": {
        "label": "プレイ済み", "category": "record", "type": "bool",
        "ops": ["=", "!="], "default": "true",
    },
    "clear": {
        "label": "クリアランプ", "category": "record", "type": "clear",
        "ops": ["=", "!=", ">=", "<=", ">", "<"], "default": "EASY",
    },
    "best_rank": {
        "label": "ベストスコアランク", "category": "record", "type": "rank",
        "ops": ["=", "!=", ">=", "<=", ">", "<"], "default": "AA",
    },
    "minbp": {
        "label": "最小ミスカウント", "category": "record", "type": "int",
        "ops": ["=", "!=", ">=", "<=", ">", "<"], "default": "50",
    },
    "miss_rate": {
        "label": "ミスカウント率(%)", "category": "record", "type": "float",
        "ops": ["=", "!=", ">=", "<=", ">", "<"], "default": "1.0",
    },
    "playcount": {
        "label": "プレイ回数", "category": "record", "type": "int",
        "ops": ["=", "!=", ">=", "<=", ">", "<"], "default": "1",
    },
    "last_play_days": {
        "label": "最終プレイからの経過日数", "category": "record", "type": "int",
        "ops": ["=", "!=", ">=", "<=", ">", "<"], "default": "30",
    },
    "score_rate": {
        "label": "ベストスコア率(%)", "category": "record", "type": "float",
        "ops": ["=", "!=", ">=", "<=", ">", "<"], "default": "80",
    },
    "density_rank": {
        "label": "同レベル内平均密度順位", "category": "analysis", "type": "density_rank",
        "ops": ["="], "default": "上位25%",
    },
    "rhythm_family": {
        "label": "リズム主体", "category": "analysis", "type": "rhythm",
        "ops": ["=", "!="], "default": "16分系",
    },
    "effective_bpm": {
        "label": "実質BPM", "category": "analysis", "type": "float",
        "ops": ["=", "!=", ">=", "<=", ">", "<"], "default": "160",
    },
    "attr": {
        "label": "第一属性", "category": "analysis", "type": "attr",
        "ops": ["=", "!="], "default": "16分乱打",
    },
    "attr_any": {
        "label": "第一or第二属性", "category": "analysis", "type": "attr",
        "ops": ["="], "default": "16分乱打",
    },
    "practice_low": {
        "label": "練習優先度低", "category": "analysis", "type": "bool",
        "ops": ["=", "!="], "default": "true",
    },
    "attr_sub": {
        "label": "副分類", "category": "analysis", "type": "attr_sub",
        "ops": ["=", "!="], "default": "中速乱打",
    },
    "attr_conf": {
        "label": "属性判定信頼度", "category": "analysis", "type": "float",
        "ops": ["=", "!=", ">=", "<=", ">", "<"], "default": "0.5",
    },
    "grid_bpm": {
        "label": "グリッドBPM(16分換算)", "category": "analysis", "type": "float",
        "ops": ["=", "!=", ">=", "<=", ">", "<"], "default": "190",
    },
    "avg_chord": {
        "label": "平均同時押し数", "category": "analysis", "type": "float",
        "ops": ["=", "!=", ">=", "<=", ">", "<"], "default": "2.5",
    },
    "micro_rate": {
        "label": "微縦連率", "category": "analysis", "type": "float",
        "ops": ["=", "!=", ">=", "<=", ">", "<"], "default": "0.075",
    },
    "subgrid_rate": {
        "label": "ズレ(ディレイ)率", "category": "analysis", "type": "float",
        "ops": ["=", "!=", ">=", "<=", ">", "<"], "default": "0.22",
    },
    "stream_sec": {
        "label": "最長発狂秒数", "category": "analysis", "type": "float",
        "ops": ["=", "!=", ">=", "<=", ">", "<"], "default": "20",
    },
    "d10": {
        "label": "10秒最大密度", "category": "analysis", "type": "float",
        "ops": ["=", "!=", ">=", "<=", ">", "<"], "default": "35",
    },
    "d20": {
        "label": "20秒最大密度", "category": "analysis", "type": "float",
        "ops": ["=", "!=", ">=", "<=", ">", "<"], "default": "33",
    },
    "last_kill": {
        "label": "ラス殺し度", "category": "analysis", "type": "float",
        "ops": ["=", "!=", ">=", "<=", ">", "<"], "default": "1.3",
    },
    "recovery_sec": {
        "label": "回復地帯秒数", "category": "analysis", "type": "float",
        "ops": ["=", "!=", ">=", "<=", ">", "<"], "default": "20",
    },
    "maker_axis_known": {
        "label": "共通難度軸で位置判明", "category": "maker", "type": "bool",
        "ops": ["=", "!="], "default": "true",
    },
    "maker_material_ok": {
        "label": "制作素材状態が良好", "category": "maker", "type": "bool",
        "ops": ["=", "!="], "default": "true",
    },
    "maker_needs_review": {
        "label": "制作素材に要確認項目あり", "category": "maker", "type": "bool",
        "ops": ["=", "!="], "default": "true",
    },
    "maker_unused_audio_count": {
        "label": "未使用候補音源数", "category": "maker", "type": "int",
        "ops": ["=", "!=", ">=", "<=", ">", "<"], "default": "1",
    },
    "maker_chart_count": {
        "label": "同曲フォルダの7KEY譜面数", "category": "maker", "type": "int",
        "ops": ["=", "!=", ">=", "<=", ">", "<"], "default": "4",
    },
    "maker_other_advanced_count": {
        "label": "同曲の他のINSANE譜面数", "category": "maker", "type": "int",
        "ops": ["=", "!=", ">=", "<=", ">", "<"], "default": "0",
    },
    "maker_has_higher": {
        "label": "同曲に上位譜面あり", "category": "maker", "type": "bool",
        "ops": ["=", "!="], "default": "false",
    },
    "maker_has_lower": {
        "label": "同曲に下位譜面あり", "category": "maker", "type": "bool",
        "ops": ["=", "!="], "default": "false",
    },
    "maker_upper_gap": {
        "label": "直近上位譜面までの難度差", "category": "maker", "type": "float",
        "ops": ["=", "!=", ">=", "<=", ">", "<"], "default": "3",
    },
    "maker_lower_gap": {
        "label": "直近下位譜面までの難度差", "category": "maker", "type": "float",
        "ops": ["=", "!=", ">=", "<=", ">", "<"], "default": "3",
    },
    "maker_has_near_higher": {
        "label": "上3段階以内に表所属譜面あり", "category": "maker", "type": "bool",
        "ops": ["=", "!="], "default": "false",
    },
    "maker_has_near_lower": {
        "label": "下3段階以内に表所属譜面あり", "category": "maker", "type": "bool",
        "ops": ["=", "!="], "default": "false",
    },
    "maker_unknown_count": {
        "label": "同曲の表外高難度譜面数", "category": "maker", "type": "int",
        "ops": ["=", "!=", ">=", "<=", ">", "<"], "default": "1",
    },
    "song_level": {
        "label": "譜面PLAYLEVEL", "category": "song", "type": "int",
        "ops": ["=", "!=", ">=", "<=", ">", "<"], "default": "12",
    },
    "notes": {
        "label": "総ノーツ数", "category": "song", "type": "int",
        "ops": ["=", "!=", ">=", "<=", ">", "<"], "default": "2000",
    },
    "minbpm": {
        "label": "最低BPM", "category": "song", "type": "int",
        "ops": ["=", "!=", ">=", "<=", ">", "<"], "default": "120",
    },
    "maxbpm": {
        "label": "最高BPM", "category": "song", "type": "int",
        "ops": ["=", "!=", ">=", "<=", ">", "<"], "default": "180",
    },
    "title": {
        "label": "曲名", "category": "text", "type": "text",
        "ops": ["含む", "含まない", "=", "!="], "default": "",
    },
    "artist": {
        "label": "アーティスト", "category": "text", "type": "text",
        "ops": ["含む", "含まない", "=", "!="], "default": "",
    },
}



class ToolError(RuntimeError):
    pass


class AnalysisCancelled(ToolError):
    """Raised when the user requests cancellation of chart analysis."""

    pass


@dataclass(frozen=True)
class HashRef:
    hash_type: str
    value: str

    def normalized(self) -> "HashRef":
        return HashRef(self.hash_type.lower(), (self.value or "").strip().lower())


@dataclass
class ChartRef:
    sha256: str = ""
    md5: str = ""
    title: str = ""
    artist: str = ""

    def normalized(self) -> "ChartRef":
        return ChartRef(
            sha256=(self.sha256 or "").strip().lower(),
            md5=(self.md5 or "").strip().lower(),
            title=self.title or "",
            artist=self.artist or "",
        )

    def preferred_key(self) -> str:
        n = self.normalized()
        if n.sha256:
            return f"sha256:{n.sha256}"
        if n.md5:
            return f"md5:{n.md5}"
        return ""

    def hash_refs(self) -> list[HashRef]:
        n = self.normalized()
        refs: list[HashRef] = []
        if n.sha256:
            refs.append(HashRef("sha256", n.sha256))
        if n.md5:
            refs.append(HashRef("md5", n.md5))
        return refs


@dataclass
class TableLevel:
    name: str
    charts: dict[str, ChartRef] = dc_field(default_factory=dict)


@dataclass
class TableInfo:
    table_id: str
    url: str
    name: str
    tag: str = ""
    source: str = "beatoraja"
    header_url: str = ""
    levels: dict[str, TableLevel] = dc_field(default_factory=dict)
    error: str = ""

    @property
    def chart_count(self) -> int:
        keys: set[str] = set()
        for level in self.levels.values():
            keys.update(level.charts.keys())
        return len(keys)

    def all_charts(self) -> dict[str, ChartRef]:
        result: dict[str, ChartRef] = {}
        for level in self.levels.values():
            merge_chart_maps(result, level.charts)
        return result


@dataclass
class Condition:
    field: str = "played"
    op: str = "="
    value: str = "true"
    enabled: bool = True
    condition_id: str = dc_field(default_factory=lambda: uuid.uuid4().hex)


@dataclass
class Preset:
    preset_id: str = dc_field(default_factory=lambda: uuid.uuid4().hex)
    name: str = "新しいフィルター"
    visible: bool = True
    join: str = "AND"
    conditions: list[Condition] = dc_field(default_factory=list)
    group_name: str = ""
    # 共有版の一覧切替単位。出力階層のgroup_nameとは独立。
    set_name: str = ""


@dataclass
class AppTableURL:
    url: str
    header_url: str = ""
    register_to_beatoraja: bool = False


def _builtin_preset_id(slug: str) -> str:
    return f"builtin:{slug}"


def make_initial_filter_presets() -> list[Preset]:
    """共有版のフィルター定義はfilter_packs/*.jsonから読み込む。"""
    return []

def is_legacy_detailed_bpm_preset(preset: Preset) -> bool:
    """Return True for the 44 detailed BPM presets bundled only in v0.6.0."""
    return str(preset.preset_id or "").startswith("builtin:rhythm-")


def disable_legacy_detailed_bpm_presets(presets: list[Preset]) -> int:
    """Hide v0.6.0's heavy detailed BPM templates once during migration.

    The presets are kept so the user can re-enable or delete them in bulk.
    """
    changed = 0
    for preset in presets:
        if is_legacy_detailed_bpm_preset(preset) and preset.visible:
            preset.visible = False
            changed += 1
    return changed


def is_maker_preset(preset: Preset) -> bool:
    return any(str(condition.field or "").startswith("maker_") for condition in preset.conditions)


def active_filter_presets(presets: Iterable[Preset], maker_enabled: bool = True) -> list[Preset]:
    return [preset for preset in presets if maker_enabled or not is_maker_preset(preset)]


def visible_filter_presets(presets: Iterable[Preset]) -> list[Preset]:
    """Return only presets that are actually emitted into default.json."""
    return [preset for preset in presets if preset.visible]


def presets_require_maker_metrics(presets: Iterable[Preset]) -> bool:
    return any(
        condition.enabled and str(condition.field or "").startswith("maker_")
        for preset in presets
        if preset.visible
        for condition in preset.conditions
    )


def presets_require_material_analysis(presets: Iterable[Preset]) -> bool:
    return any(
        condition.enabled and condition.field in MATERIAL_MAKER_FIELDS
        for preset in presets
        if preset.visible
        for condition in preset.conditions
    )


def merge_initial_filter_presets(existing: list[Preset]) -> tuple[list[Preset], int]:
    """Append missing bundled templates without replacing user edits."""
    result = list(existing)
    ids = {p.preset_id for p in result if p.preset_id}
    names = {(p.group_name.strip(), p.name.strip()) for p in result}

    def signature(preset: Preset) -> tuple[str, tuple[tuple[str, str, str, bool], ...]]:
        conditions = tuple(sorted(
            (str(c.field), str(c.op), str(c.value), bool(c.enabled)) for c in preset.conditions
        ))
        return (str(preset.join or "AND").upper(), conditions)

    signatures = {signature(p) for p in result}
    added = 0
    for preset in make_initial_filter_presets():
        key = (preset.group_name.strip(), preset.name.strip())
        preset_signature = signature(preset)
        if preset.preset_id in ids or key in names or preset_signature in signatures:
            continue
        result.append(preset)
        ids.add(preset.preset_id)
        names.add(key)
        signatures.add(preset_signature)
        added += 1
    return result, added



FILTER_PACK_FORMAT = "bms-filter-pack-v2"
FILTER_PACK_ID_PREFIX = "pack:"
PACK_EDITABLE_TYPES = {"int", "float"}


def _version_key(value: str) -> tuple[int, ...]:
    parts = re.findall(r"\d+", str(value or ""))
    return tuple(int(part) for part in parts[:4]) or (0,)


def _filter_pack_preset(pack_id: str, raw: dict[str, Any], source: Path) -> Preset:
    local_id = str(raw.get("preset_id") or "").strip()
    if not local_id:
        raise ToolError(f"{source.name}: preset_idが空です")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", local_id):
        raise ToolError(f"{source.name}: preset_idには英数字・._-のみ使用できます: {local_id}")
    preset_id = f"{FILTER_PACK_ID_PREFIX}{pack_id}:{local_id}"
    name = str(raw.get("name") or "").strip()
    if not name:
        raise ToolError(f"{source.name}: {local_id} のnameが空です")
    set_name = str(raw.get("set_name") or "").strip()
    if not set_name:
        raise ToolError(f"{source.name}: {local_id} のset_nameが空です")
    join = str(raw.get("join") or "AND").upper()
    if join not in {"AND", "OR"}:
        raise ToolError(f"{source.name}: {local_id} のjoinはAND/ORのみです")
    conditions: list[Condition] = []
    for index, item in enumerate(raw.get("conditions") or [], start=1):
        if not isinstance(item, dict):
            raise ToolError(f"{source.name}: {local_id} の条件{index}がオブジェクトではありません")
        field_name = str(item.get("field") or "").strip()
        if field_name not in FIELD_DEFS:
            raise ToolError(f"{source.name}: {local_id} の未対応項目です: {field_name}")
        if FIELD_DEFS[field_name].get("category") == "maker":
            raise ToolError(f"{source.name}: 共有版では差分制作項目を使用できません: {field_name}")
        op = str(item.get("op") or "=").strip()
        if op not in FIELD_DEFS[field_name].get("ops", []):
            raise ToolError(f"{source.name}: {local_id} / {field_name} の比較方法が不正です: {op}")
        local_condition_id = str(item.get("condition_id") or f"condition-{index}").strip()
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", local_condition_id):
            raise ToolError(
                f"{source.name}: {local_id} のcondition_idには英数字・._-のみ使用できます: "
                f"{local_condition_id}"
            )
        conditions.append(Condition(
            field=field_name,
            op=op,
            value=str(item.get("value") if item.get("value") is not None else ""),
            enabled=bool(item.get("enabled", True)),
            condition_id=f"{preset_id}:{local_condition_id}",
        ))
    if not conditions:
        raise ToolError(f"{source.name}: {local_id} に条件がありません")
    return Preset(
        preset_id=preset_id,
        name=name,
        visible=bool(raw.get("default_enabled", raw.get("visible", True))),
        join=join,
        conditions=conditions,
        group_name=str(raw.get("group_name") or "").strip(),
        set_name=set_name,
    )


def load_filter_preset_packs(
    pack_dir: Path,
) -> tuple[list[Preset], list[str], int, list[dict[str, str]]]:
    """外部フィルターパックを検証して読み込む。エラー時は全体を適用しない。"""
    presets: list[Preset] = []
    messages: list[str] = []
    pack_infos: list[dict[str, str]] = []
    file_count = 0
    seen_pack_ids: set[str] = set()
    seen_preset_ids: set[str] = set()
    if not pack_dir.exists():
        return presets, messages, file_count, pack_infos
    for path in sorted(pack_dir.glob("*.json"), key=lambda item: item.name.casefold()):
        file_count += 1
        try:
            raw = safe_load_json(path, None)
            if not isinstance(raw, dict):
                raise ToolError(f"{path.name}: JSONルートがオブジェクトではありません")
            if raw.get("format") != FILTER_PACK_FORMAT:
                raise ToolError(f"{path.name}: formatが未対応です")
            if not bool(raw.get("enabled", True)):
                continue
            pack_id = str(raw.get("pack_id") or path.stem).strip()
            if not re.fullmatch(r"[A-Za-z0-9_.-]+", pack_id):
                raise ToolError(f"{path.name}: pack_idには英数字・._-のみ使用できます")
            if pack_id in seen_pack_ids:
                raise ToolError(f"{path.name}: pack_idが他ファイルと重複しています: {pack_id}")
            minimum = str(raw.get("min_tool_version") or "").strip()
            if minimum and _version_key(APP_VERSION) < _version_key(minimum):
                raise ToolError(
                    f"{path.name}: ツールv{minimum}以上が必要です（現在v{APP_VERSION}）"
                )
            local: list[Preset] = []
            for item in raw.get("presets") or []:
                if not isinstance(item, dict):
                    raise ToolError(f"{path.name}: presets内にオブジェクト以外があります")
                preset = _filter_pack_preset(pack_id, item, path)
                if preset.preset_id in seen_preset_ids:
                    raise ToolError(f"{path.name}: preset_idが重複しています: {preset.preset_id}")
                local.append(preset)
            if not local:
                raise ToolError(f"{path.name}: 有効なpresetsがありません")
            seen_pack_ids.add(pack_id)
            for preset in local:
                seen_preset_ids.add(preset.preset_id)
            presets.extend(local)
            pack_infos.append({
                "pack_id": pack_id,
                "name": str(raw.get("name") or pack_id),
                "version": str(raw.get("pack_version") or ""),
                "file": path.name,
                "description": str(raw.get("description") or ""),
            })
        except Exception as exc:
            messages.append(str(exc))
    return presets, messages, file_count, pack_infos


def sync_filter_preset_packs(
    existing: list[Preset],
    pack_dir: Path,
    *,
    preserve_numeric_values: bool = True,
    pack_only: bool = True,
) -> tuple[list[Preset], dict[str, Any]]:
    """パック定義へ同期する。

    名前・構造・固定条件はパック側を採用し、利用者が小型画面で変更できる
    数値条件だけは既存値を維持する。エラーが1件でもあれば既存設定を維持する。
    """
    loaded, messages, file_count, pack_infos = load_filter_preset_packs(pack_dir)
    if messages or file_count == 0:
        if file_count == 0:
            messages = [f"フィルターパックがありません: {pack_dir}"]
        return list(existing), {
            "applied": False,
            "files": file_count,
            "packs": len(pack_infos),
            "loaded": len(loaded),
            "added": 0,
            "updated": 0,
            "removed": 0,
            "messages": messages,
            "pack_infos": pack_infos,
        }

    existing_by_id = {preset.preset_id: preset for preset in existing if preset.preset_id}
    result: list[Preset] = []
    added = updated = 0
    loaded_ids = {preset.preset_id for preset in loaded}
    for preset in loaded:
        old = existing_by_id.get(preset.preset_id)
        if old is None:
            result.append(preset)
            added += 1
            continue
        preset.visible = old.visible
        if preserve_numeric_values:
            old_by_id = {condition.condition_id: condition for condition in old.conditions}
            old_by_signature: dict[tuple[str, str], list[Condition]] = {}
            for condition in old.conditions:
                old_by_signature.setdefault((condition.field, condition.op), []).append(condition)
            used_fallback: dict[tuple[str, str], int] = {}
            for condition in preset.conditions:
                info = FIELD_DEFS.get(condition.field, {})
                if info.get("type") not in PACK_EDITABLE_TYPES:
                    continue
                previous = old_by_id.get(condition.condition_id)
                if previous is None:
                    key = (condition.field, condition.op)
                    candidates = old_by_signature.get(key, [])
                    pos = used_fallback.get(key, 0)
                    if pos < len(candidates):
                        previous = candidates[pos]
                        used_fallback[key] = pos + 1
                if previous is not None:
                    default_value = str(condition.value)
                    condition.value = previous.value
                    if default_value != str(previous.value) and default_value and default_value in preset.name:
                        preset.name = preset.name.replace(default_value, str(previous.value), 1)
        result.append(preset)
        updated += 1

    if not pack_only:
        result.extend(
            preset for preset in existing
            if not str(preset.preset_id or "").startswith(FILTER_PACK_ID_PREFIX)
        )
    old_pack_ids = {
        preset.preset_id for preset in existing
        if str(preset.preset_id or "").startswith(FILTER_PACK_ID_PREFIX)
    }
    removed = len(old_pack_ids - loaded_ids)
    return result, {
        "applied": True,
        "files": file_count,
        "packs": len(pack_infos),
        "loaded": len(loaded),
        "added": added,
        "updated": updated,
        "removed": removed,
        "messages": [],
        "pack_infos": pack_infos,
    }

def upgrade_builtin_maker_presets(presets: list[Preset]) -> int:
    """Refresh bundled maker presets while preserving each user's visibility choice."""
    desired = {
        preset.preset_id: preset
        for preset in make_initial_filter_presets()
        if str(preset.preset_id or "").startswith("builtin:maker-")
    }
    changed = 0
    for preset in presets:
        replacement = desired.get(preset.preset_id)
        if replacement is None:
            continue
        visible = preset.visible
        preset.name = replacement.name
        preset.group_name = replacement.group_name
        preset.join = replacement.join
        preset.conditions = replacement.conditions
        preset.visible = visible
        changed += 1
    return changed


@dataclass
class TableCombination:
    combination_id: str = dc_field(default_factory=lambda: uuid.uuid4().hex)
    name: str = "掛け合わせ難易度表"
    visible: bool = True
    # v0.8以降。table:<table_id> / combo:<combination_id> を入力順に保持する。
    # 先頭入力がレベル表示の基準になる方式では、この順序に意味がある。
    input_refs: list[str] = dc_field(default_factory=list)
    method: str = "plain"  # plain / intersect / union_same / merge_same / level_signature
    # v0.7以前の設定読込・外部コード互換用。保存時も残るが、実処理はinput_refsを使用する。
    base_table_id: str = ""
    cross_table_id: str = ""


@dataclass
class DefaultFolderEntry:
    entry_id: str = dc_field(default_factory=lambda: uuid.uuid4().hex)
    name: str = "カスタムフォルダ"
    visible: bool = True
    data: dict[str, Any] = dc_field(default_factory=dict)


@dataclass
class DefaultFolderProfile:
    profile_key: str = ""
    root: str = ""
    label: str = "beatoraja"
    default_folders: list[DefaultFolderEntry] = dc_field(default_factory=list)


@dataclass
class AppSettings:
    version: int = SETTINGS_VERSION
    rian_root: str = ""
    player_name: str = "player1"
    base_table_ids: list[str] = dc_field(default_factory=list)  # v0.6以前の移行用
    cross_table_ids: list[str] = dc_field(default_factory=list)  # v0.6以前の移行用
    table_combinations: list[TableCombination] = dc_field(default_factory=list)
    default_folders: list[DefaultFolderEntry] = dc_field(default_factory=list)  # v0.7.0以前の移行用
    default_folder_profiles: list[DefaultFolderProfile] = dc_field(default_factory=list)
    managed_folder_names: list[str] = dc_field(default_factory=lambda: [MANAGED_ROOT_NAME])
    app_tables: list[AppTableURL] = dc_field(default_factory=list)
    presets: list[Preset] = dc_field(default_factory=make_initial_filter_presets)
    root_folder_name: str = MANAGED_ROOT_NAME
    quick_topmost: bool = True
    instant_density_table_enabled: bool = False
    maker_table_enabled: bool = False
    maker_diagnostic_enabled: bool = False
    maker_diagnostic_topmost: bool = True
    random_course_enabled: bool = False
    random_course_filter_enabled: bool = True
    random_course_stages: int = 4
    random_course_distinct: bool = True
    random_course_folder_name: str = "ランダムコース"
    random_course_name: str = "ALL RANDOM {count}"
    random_course_filter_name: str = "{filter} RANDOM {count}"
    current_song_history_path: str = ""
    # 共有・シンプルモード用。詳細編集側のtable_combinations / preset.visibleは変更せず、
    # 日常運用の対象表・フィルター選択を別に保持する。
    simple_selected_table_ids: list[str] = dc_field(default_factory=list)
    simple_table_selection_initialized: bool = False
    # simple_filter_set は最後に注目していたセット名（旧版互換・表示用）。
    # 実際の生成対象は simple_selected_filter_sets で複数保持する。
    simple_filter_set: str = ""
    simple_selected_filter_sets: list[str] = dc_field(default_factory=list)
    simple_filter_set_selection_initialized: bool = False
    simple_known_filter_sets: list[str] = dc_field(default_factory=list)
    simple_filter_set_order: list[str] = dc_field(default_factory=lambda: list(DEFAULT_SIMPLE_FILTER_SET_ORDER))
    simple_preset_enabled: dict[str, bool] = dc_field(default_factory=dict)
    main_geometry: str = "1120x760"
    simple_geometry: str = "1180x760"
    quick_geometry: str = "430x400"
    maker_diagnostic_geometry: str = "410x360"
    update_check_on_startup: bool = True
    last_update_check_ts: int = 0

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AppSettings":
        try:
            source_version = int(data.get("version") or 0)
        except (TypeError, ValueError):
            source_version = 0
        presets = []
        for p in data.get("presets", []):
            conditions = [Condition(**c) for c in p.get("conditions", [])]
            presets.append(Preset(
                preset_id=p.get("preset_id") or uuid.uuid4().hex,
                name=p.get("name", "フィルター"),
                visible=bool(p.get("visible", True)),
                join=(p.get("join", "AND") or "AND").upper(),
                conditions=conditions,
                group_name=str(p.get("group_name", "") or "").strip(),
                set_name=str(p.get("set_name", "") or "").strip(),
            ))
        combinations = []
        for item in data.get("table_combinations", []):
            if not isinstance(item, dict):
                continue
            combinations.append(TableCombination(
                combination_id=str(item.get("combination_id") or uuid.uuid4().hex),
                name=str(item.get("name") or "掛け合わせ難易度表"),
                visible=bool(item.get("visible", True)),
                input_refs=[str(x) for x in item.get("input_refs", []) if str(x).strip()],
                base_table_id=str(item.get("base_table_id") or ""),
                method=str(item.get("method") or "plain"),
                cross_table_id=str(item.get("cross_table_id") or ""),
            ))
        default_folders = []
        for item in data.get("default_folders", []):
            if not isinstance(item, dict):
                continue
            payload = item.get("data") if isinstance(item.get("data"), dict) else {}
            default_folders.append(DefaultFolderEntry(
                entry_id=str(item.get("entry_id") or uuid.uuid4().hex),
                name=str(item.get("name") or payload.get("name") or "カスタムフォルダ"),
                visible=bool(item.get("visible", True)),
                data=payload,
            ))
        default_folder_profiles = []
        for profile in data.get("default_folder_profiles", []):
            if not isinstance(profile, dict):
                continue
            folders = []
            for item in profile.get("default_folders", []):
                if not isinstance(item, dict):
                    continue
                payload = item.get("data") if isinstance(item.get("data"), dict) else {}
                folders.append(DefaultFolderEntry(
                    entry_id=str(item.get("entry_id") or uuid.uuid4().hex),
                    name=str(item.get("name") or payload.get("name") or "カスタムフォルダ"),
                    visible=bool(item.get("visible", True)),
                    data=payload,
                ))
            default_folder_profiles.append(DefaultFolderProfile(
                profile_key=str(profile.get("profile_key") or ""),
                root=str(profile.get("root") or ""),
                label=str(profile.get("label") or "beatoraja"),
                default_folders=folders,
            ))
        app_tables = []
        for t in data.get("app_tables", []):
            if isinstance(t, str):
                app_tables.append(AppTableURL(url=t))
            elif isinstance(t, dict) and t.get("url"):
                app_tables.append(AppTableURL(
                    url=t.get("url", ""),
                    header_url=t.get("header_url", ""),
                    register_to_beatoraja=bool(t.get("register_to_beatoraja", False)),
                ))
        if not presets:
            presets = make_initial_filter_presets()
        elif source_version < SETTINGS_VERSION:
            presets, _added = merge_initial_filter_presets(presets)
            # v0.6.0で同梱した詳細BPM 44件は生成負荷が大きいため、
            # 削除せず一度だけ非表示へ移行する。
            if source_version <= 6:
                disable_legacy_detailed_bpm_presets(presets)
            if source_version <= 8:
                upgrade_builtin_maker_presets(presets)
        quick_geometry = str(data.get("quick_geometry", "430x400") or "430x400")
        if source_version < 14:
            match = re.match(r"^(\d+)x(\d+)", quick_geometry)
            if not match or int(match.group(1)) < 390 or int(match.group(2)) < 320:
                quick_geometry = "430x400"
        # v1.0.15: 小型パネルを数値条件専用へ縮小。旧既定サイズだけ新サイズへ移行する。
        if source_version < 23 and quick_geometry.startswith("560x560"):
            quick_geometry = "430x400"
        simple_geometry = str(data.get("simple_geometry", "1180x760") or "1180x760")
        if source_version < 16:
            match = re.match(r"^(\d+)x(\d+)", simple_geometry)
            if not match or int(match.group(1)) < 960 or int(match.group(2)) < 740:
                simple_geometry = "1180x760"
        simple_preset_enabled = {
            str(key): bool(value)
            for key, value in (data.get("simple_preset_enabled", {}) or {}).items()
            if str(key).strip()
        }
        # v1.0.10: 公開画面の「スコア」を「スコア状況」へ統一。
        # 既存利用者の選択状態・並び順も同名へ移行する。
        def _rename_simple_set(value: Any) -> str:
            text = str(value or "")
            return "スコア状況" if text == "スコア" else text

        migrated_simple_filter_set = _rename_simple_set(data.get("simple_filter_set", ""))
        migrated_selected_filter_sets = [
            _rename_simple_set(x) for x in data.get("simple_selected_filter_sets", []) if str(x).strip()
        ]
        migrated_known_filter_sets = [
            _rename_simple_set(x) for x in data.get("simple_known_filter_sets", []) if str(x).strip()
        ]
        migrated_filter_set_order = [
            _rename_simple_set(x) for x in data.get("simple_filter_set_order", []) if str(x).strip()
        ]
        # 重複を保ったままにしない。
        migrated_selected_filter_sets = list(dict.fromkeys(migrated_selected_filter_sets))
        migrated_known_filter_sets = list(dict.fromkeys(migrated_known_filter_sets))
        migrated_filter_set_order = list(dict.fromkeys(migrated_filter_set_order))
        # v1.0.14: 未変更の旧初期順だけ、新しい公開版の初期順へ移行する。
        # 利用者が自分で並べ替えた順序は保持する。
        if source_version < 22 and (
            not migrated_filter_set_order
            or migrated_filter_set_order == LEGACY_SIMPLE_FILTER_SET_ORDER
        ):
            migrated_filter_set_order = list(DEFAULT_SIMPLE_FILTER_SET_ORDER)
        # v1.0.9: detailed tendency subdivisions are opt-in in the public edition.
        if source_version < 19:
            detailed_groups = {"腕ガチ詳細", "指ガチ詳細", "16分乱打詳細", "ディレイ詳細"}
            for preset in presets:
                if str(preset.group_name or "").strip() in detailed_groups:
                    preset.visible = False
                    simple_preset_enabled[preset.preset_id] = False

        return cls(
            version=SETTINGS_VERSION,
            rian_root=str(data.get("rian_root", "") or ""),
            player_name=str(data.get("player_name", "player1") or "player1"),
            base_table_ids=list(data.get("base_table_ids", [])),
            cross_table_ids=list(data.get("cross_table_ids", [])),
            table_combinations=combinations,
            default_folders=default_folders,
            default_folder_profiles=default_folder_profiles,
            managed_folder_names=list(data.get("managed_folder_names", [MANAGED_ROOT_NAME])),
            app_tables=app_tables,
            presets=presets,
            root_folder_name=data.get("root_folder_name", MANAGED_ROOT_NAME),
            quick_topmost=bool(data.get("quick_topmost", True)),
            instant_density_table_enabled=False,
            maker_table_enabled=False,
            maker_diagnostic_enabled=bool(data.get("maker_diagnostic_enabled", False)),
            maker_diagnostic_topmost=bool(data.get("maker_diagnostic_topmost", True)),
            random_course_enabled=False,
            random_course_filter_enabled=True,
            random_course_stages=max(1, min(20, int(data.get("random_course_stages", 4) or 4))),
            random_course_distinct=bool(data.get("random_course_distinct", True)),
            random_course_folder_name=str(data.get("random_course_folder_name", "ランダムコース") or "ランダムコース"),
            random_course_name=str(data.get("random_course_name", "ALL RANDOM {count}") or "ALL RANDOM {count}"),
            random_course_filter_name=str(data.get("random_course_filter_name", "{filter} RANDOM {count}") or "{filter} RANDOM {count}"),
            current_song_history_path=str(data.get("current_song_history_path", "") or ""),
            simple_selected_table_ids=[str(x) for x in data.get("simple_selected_table_ids", []) if str(x).strip()],
            simple_table_selection_initialized=bool(data.get("simple_table_selection_initialized", False)),
            simple_filter_set=migrated_simple_filter_set,
            simple_selected_filter_sets=migrated_selected_filter_sets,
            simple_filter_set_selection_initialized=bool(
                data.get("simple_filter_set_selection_initialized", False)
            ),
            simple_known_filter_sets=migrated_known_filter_sets,
            simple_filter_set_order=migrated_filter_set_order,
            simple_preset_enabled=simple_preset_enabled,
            main_geometry=data.get("main_geometry", "1120x760"),
            simple_geometry=simple_geometry,
            quick_geometry=quick_geometry,
            maker_diagnostic_geometry=data.get("maker_diagnostic_geometry", "410x360"),
            update_check_on_startup=bool(data.get("update_check_on_startup", True)),
            last_update_check_ts=max(0, int(data.get("last_update_check_ts", 0) or 0)),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


COMBINATION_METHOD_LABELS = {
    "plain": "先頭入力をそのまま",
    "intersect": "全入力に共通する譜面",
    "exclude_others": "先頭入力から他入力を除外",
    "union_same": "先頭入力＋他入力の同番号レベル",
    "merge_same": "全入力を同番号レベルで集約",
    "level_signature": "所属レベルの組み合わせごとに分類",
}


def combination_method_label(method: str) -> str:
    return COMBINATION_METHOD_LABELS.get(method, method or "先頭入力をそのまま")


_SIMPLE_ATTRIBUTE_FIELDS = {
    "attr", "attr_any", "attr_sub", "attr_conf", "practice_low",
    "grid_bpm", "avg_chord", "micro_rate", "subgrid_rate",
    "stream_sec", "d10", "d20", "last_kill", "recovery_sec",
}

def simple_filter_set_name(preset: Preset) -> str:
    """Return the pack-defined list switch name without changing folder grouping."""
    explicit = str(getattr(preset, "set_name", "") or "").strip()
    if explicit:
        return explicit
    fields = {condition.field for condition in preset.conditions}
    if fields & _SIMPLE_ATTRIBUTE_FIELDS:
        return "譜面傾向"
    group = str(preset.group_name or "").strip()
    return group or "その他"


def simple_filter_sets(presets: Iterable[Preset], *, include_maker: bool = False) -> list[str]:
    result: list[str] = []
    for preset in presets:
        fields = {condition.field for condition in preset.conditions}
        if not include_maker and any(FIELD_DEFS.get(field, {}).get("category") == "maker" for field in fields):
            continue
        name = simple_filter_set_name(preset)
        if name not in result:
            result.append(name)
    return result


def ordered_simple_filter_sets(settings: AppSettings, *, include_maker: bool = False) -> list[str]:
    """Return filter sets in the user's public-UI order."""
    available = simple_filter_sets(settings.presets, include_maker=include_maker)
    available_set = set(available)
    source_order = settings.simple_filter_set_order or DEFAULT_SIMPLE_FILTER_SET_ORDER
    ordered = [name for name in source_order if name in available_set]
    # 標準セットは、設定ファイル内の記載順に左右されないよう既定順で補う。
    ordered.extend(name for name in DEFAULT_SIMPLE_FILTER_SET_ORDER if name in available_set and name not in ordered)
    ordered.extend(name for name in available if name not in ordered)
    return ordered


def simple_presets_for_set(
    presets: Iterable[Preset], filter_set: str, *, include_maker: bool = False
) -> list[Preset]:
    result: list[Preset] = []
    for preset in presets:
        fields = {condition.field for condition in preset.conditions}
        if not include_maker and any(FIELD_DEFS.get(field, {}).get("category") == "maker" for field in fields):
            continue
        if simple_filter_set_name(preset) == filter_set:
            result.append(preset)
    return result


def simple_presets_for_sets(
    presets: Iterable[Preset], filter_sets: Iterable[str], *, include_maker: bool = False
) -> list[Preset]:
    selected = {str(name).strip() for name in filter_sets if str(name).strip()}
    if not selected:
        return []
    return [
        preset for preset in presets
        if simple_filter_set_name(preset) in selected
        and (include_maker or not any(
            FIELD_DEFS.get(condition.field, {}).get("category") == "maker"
            for condition in preset.conditions
        ))
    ]


def initialize_simple_filter_set_selection(settings: AppSettings) -> bool:
    """Keep the multi-select filter-set state valid and enable newly delivered sets once."""
    changed = False
    raw_available = simple_filter_sets(settings.presets)
    raw_set = set(raw_available)
    source_order = settings.simple_filter_set_order or DEFAULT_SIMPLE_FILTER_SET_ORDER
    stored_order = [name for name in source_order if name in raw_set]
    stored_order.extend(name for name in DEFAULT_SIMPLE_FILTER_SET_ORDER if name in raw_set and name not in stored_order)
    stored_order.extend(name for name in raw_available if name not in stored_order)
    if stored_order != settings.simple_filter_set_order:
        settings.simple_filter_set_order = list(stored_order)
        changed = True
    available = list(stored_order)
    available_set = set(available)
    known = {name for name in settings.simple_known_filter_sets if name in available_set}
    selected = [name for name in settings.simple_selected_filter_sets if name in available_set]

    if not settings.simple_filter_set_selection_initialized:
        # v1.0.2以前の単一プルダウンは編集対象の切替であり、
        # 他セットを明示的に除外した操作ではないため、初回移行では全セットを有効にする。
        selected = list(available)
        settings.simple_filter_set_selection_initialized = True
        changed = True
    else:
        # フィルターパック更新で新しいセットが追加された場合は自動的に対象へ加える。
        for name in available:
            if name not in known and name not in selected:
                selected.append(name)
                changed = True

    ordered = [name for name in available if name in set(selected)]
    if ordered != settings.simple_selected_filter_sets:
        settings.simple_selected_filter_sets = ordered
        changed = True
    if available != settings.simple_known_filter_sets:
        settings.simple_known_filter_sets = list(available)
        changed = True
    if settings.simple_filter_set not in available_set:
        settings.simple_filter_set = (
            settings.simple_selected_filter_sets[0]
            if settings.simple_selected_filter_sets
            else (available[0] if available else "")
        )
        changed = True
    return changed


def initialize_simple_selection(settings: AppSettings, tables: Iterable[TableInfo]) -> bool:
    """Initialize simple-mode defaults once. Empty after initialization means intentionally none."""
    changed = False
    valid_tables = [table for table in tables if not table.error and table.levels]
    valid_ids = {table.table_id for table in valid_tables}
    cleaned = [table_id for table_id in settings.simple_selected_table_ids if table_id in valid_ids]
    if cleaned != settings.simple_selected_table_ids:
        settings.simple_selected_table_ids = cleaned
        changed = True
    if not settings.simple_table_selection_initialized:
        selected: list[str] = []
        for combo in settings.table_combinations:
            if not combo.visible or combo.method != "plain":
                continue
            refs = combination_input_refs(combo)
            if len(refs) != 1:
                continue
            kind, value = split_source_ref(refs[0])
            if kind == "table" and value in valid_ids and value not in selected:
                selected.append(value)
        if not selected:
            selected = [table.table_id for table in valid_tables]
        settings.simple_selected_table_ids = selected
        settings.simple_table_selection_initialized = True
        changed = True
    if initialize_simple_filter_set_selection(settings):
        changed = True
    for preset in settings.presets:
        if preset.preset_id not in settings.simple_preset_enabled:
            settings.simple_preset_enabled[preset.preset_id] = bool(preset.visible)
            changed = True
    return changed


def build_simple_runtime_settings(settings: AppSettings, tables: Iterable[TableInfo]) -> AppSettings:
    """Create generation settings for registered-table + selected-filter simple operation."""
    runtime = copy.deepcopy(settings)
    valid = {table.table_id: table for table in tables if not table.error and table.levels}
    selected = [table_id for table_id in settings.simple_selected_table_ids if table_id in valid]
    name_counts: dict[str, int] = {}
    runtime.table_combinations = []
    for table_id in selected:
        base_name = valid[table_id].name.strip() or table_id
        name_counts[base_name] = name_counts.get(base_name, 0) + 1
        display_name = base_name if name_counts[base_name] == 1 else f"{base_name} ({name_counts[base_name]})"
        runtime.table_combinations.append(TableCombination(
            combination_id=f"simple:{table_id}",
            name=display_name,
            visible=True,
            input_refs=[table_source_ref(table_id)],
            base_table_id=table_id,
            method="plain",
        ))
    selected_sets = set(settings.simple_selected_filter_sets)
    order = {name: index for index, name in enumerate(ordered_simple_filter_sets(settings))}
    indexed_presets = list(enumerate(runtime.presets))
    indexed_presets.sort(key=lambda item: (order.get(simple_filter_set_name(item[1]), len(order)), item[0]))
    runtime.presets = [preset for _index, preset in indexed_presets]
    for preset in runtime.presets:
        preset.visible = (
            simple_filter_set_name(preset) in selected_sets
            and bool(settings.simple_preset_enabled.get(preset.preset_id, preset.visible))
        )
    # 共有版は登録済み難易度表へのフィルター適用に限定する。
    runtime.instant_density_table_enabled = False
    runtime.maker_table_enabled = False
    runtime.random_course_enabled = False
    # 公開版では、有効な各フィルター結果にランダムコースを用意する。
    # コースは build_folder_tree() が「ランダムコース」子フォルダーへ隔離する。
    runtime.random_course_filter_enabled = True
    return runtime


def table_source_ref(table_id: str) -> str:
    return f"table:{table_id}" if table_id else ""


def combination_source_ref(combination_id: str) -> str:
    return f"combo:{combination_id}" if combination_id else ""


def split_source_ref(ref: str) -> tuple[str, str]:
    text = str(ref or "").strip()
    if text.startswith("table:"):
        return "table", text[6:]
    if text.startswith("combo:"):
        return "combo", text[6:]
    # v0.7互換。型指定のないIDは元難易度表として扱う。
    return "table", text


def combination_input_refs(combo: TableCombination) -> list[str]:
    refs = [str(x).strip() for x in combo.input_refs if str(x).strip()]
    if not refs and combo.base_table_id:
        refs.append(table_source_ref(combo.base_table_id))
        if combo.method != "plain" and combo.cross_table_id:
            refs.append(table_source_ref(combo.cross_table_id))
    result: list[str] = []
    seen: set[str] = set()
    for ref in refs:
        kind, value = split_source_ref(ref)
        normalized = (table_source_ref(value) if kind == "table" else combination_source_ref(value))
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    if combo.method == "plain" and result:
        return result[:1]
    return result


def sync_legacy_combination_fields(combo: TableCombination) -> None:
    refs = combination_input_refs(combo)
    combo.input_refs = refs
    combo.base_table_id = ""
    combo.cross_table_id = ""
    if refs:
        kind, value = split_source_ref(refs[0])
        if kind == "table":
            combo.base_table_id = value
    if len(refs) > 1:
        kind, value = split_source_ref(refs[1])
        if kind == "table":
            combo.cross_table_id = value


def validate_combination_graph(combinations: Iterable[TableCombination]) -> None:
    combos = {combo.combination_id: combo for combo in combinations if combo.combination_id}
    state: dict[str, int] = {}
    stack: list[str] = []

    def visit(combo_id: str) -> None:
        mark = state.get(combo_id, 0)
        if mark == 2:
            return
        if mark == 1:
            cycle_start = stack.index(combo_id) if combo_id in stack else 0
            ids = stack[cycle_start:] + [combo_id]
            names = [combos.get(cid).name if combos.get(cid) else cid for cid in ids]
            raise ToolError("掛け合わせ済み難易度表が循環参照しています: " + " → ".join(names))
        combo = combos.get(combo_id)
        if combo is None:
            return
        state[combo_id] = 1
        stack.append(combo_id)
        for ref in combination_input_refs(combo):
            kind, value = split_source_ref(ref)
            if kind != "combo":
                continue
            if value == combo_id:
                raise ToolError(f"『{combo.name}』が自分自身を入力にしています")
            if value not in combos:
                raise ToolError(f"『{combo.name}』が存在しない生成済み表を参照しています: {value}")
            visit(value)
        stack.pop()
        state[combo_id] = 2

    for combo_id in combos:
        visit(combo_id)


def ensure_table_combinations(settings: AppSettings, tables: list[TableInfo]) -> int:
    """旧設定を多入力・多段合成形式へ移行し、入力参照を正規化する。"""
    by_id = {table.table_id: table for table in tables if not table.error and table.levels}
    changed = 0
    valid_methods = set(COMBINATION_METHOD_LABELS)
    cleaned: list[TableCombination] = []
    for combo in settings.table_combinations:
        if combo.method not in valid_methods:
            combo.method = "plain"
            changed += 1
        before = list(combo.input_refs)
        refs = combination_input_refs(combo)
        self_ref = combination_source_ref(combo.combination_id)
        refs = [ref for ref in refs if ref != self_ref]
        if combo.method == "plain" and refs:
            refs = refs[:1]
        combo.input_refs = refs
        sync_legacy_combination_fields(combo)
        if before != combo.input_refs:
            changed += 1
        if not combo.name.strip():
            base_name = ""
            if refs:
                kind, value = split_source_ref(refs[0])
                if kind == "table":
                    base = by_id.get(value)
                    base_name = base.name if base else value
            combo.name = base_name or "掛け合わせ難易度表"
            changed += 1
        cleaned.append(combo)
    settings.table_combinations = cleaned
    if settings.table_combinations:
        settings.base_table_ids = []
        settings.cross_table_ids = []
        validate_combination_graph(settings.table_combinations)
        return changed

    legacy_base_ids = list(settings.base_table_ids)
    legacy_cross_ids = list(settings.cross_table_ids)
    for base_id in legacy_base_ids:
        base = by_id.get(base_id)
        base_name = base.name if base else base_id
        settings.table_combinations.append(TableCombination(
            name=base_name or "難易度表", input_refs=[table_source_ref(base_id)],
            base_table_id=base_id, method="plain"
        ))
        changed += 1
        for cross_id in legacy_cross_ids:
            if cross_id == base_id:
                continue
            cross = by_id.get(cross_id)
            cross_name = cross.name if cross else cross_id
            refs = [table_source_ref(base_id), table_source_ref(cross_id)]
            settings.table_combinations.append(TableCombination(
                name=f"{base_name} ∩ {cross_name}", input_refs=refs,
                base_table_id=base_id, method="intersect", cross_table_id=cross_id,
            ))
            settings.table_combinations.append(TableCombination(
                name=f"{base_name}＋{cross_name}同レベル", input_refs=refs,
                base_table_id=base_id, method="union_same", cross_table_id=cross_id,
            ))
            changed += 2
    settings.base_table_ids = []
    settings.cross_table_ids = []
    return changed


def combination_table_ids(combinations: Iterable[TableCombination], visible_only: bool = True) -> list[str]:
    combos = {combo.combination_id: combo for combo in combinations if combo.combination_id}
    result: list[str] = []
    seen_tables: set[str] = set()
    seen_combos: set[str] = set()

    def collect_combo(combo: TableCombination) -> None:
        if combo.combination_id in seen_combos:
            return
        seen_combos.add(combo.combination_id)
        for ref in combination_input_refs(combo):
            kind, value = split_source_ref(ref)
            if kind == "combo":
                dependency = combos.get(value)
                if dependency is not None:
                    collect_combo(dependency)
            elif value and value not in {INSTANT_TABLE_ID, MAKER_TABLE_ID} and value not in seen_tables:
                seen_tables.add(value)
                result.append(value)

    for combo in combinations:
        if visible_only and not combo.visible:
            continue
        collect_combo(combo)
    return result


def generated_combinations(settings: AppSettings) -> list[TableCombination]:
    # 非表示の中間表も、表示中の派生表から参照される可能性があるため解決対象へ含める。
    result = list(settings.table_combinations)
    if settings.instant_density_table_enabled:
        result.append(TableCombination(
            combination_id="builtin:instant-density",
            name="表外差分即席難易度表",
            visible=True,
            input_refs=[table_source_ref(INSTANT_TABLE_ID)],
            base_table_id=INSTANT_TABLE_ID,
            method="plain",
        ))
    return result

def _entry_name(payload: dict[str, Any], index: int) -> str:
    return str(payload.get("name") or f"名称なしカスタムフォルダ {index + 1}")


def environment_profile_key(root: Path) -> str:
    """環境ごとにdefault.json表示設定を分離するための安定キー。"""
    try:
        resolved = root.expanduser().resolve()
    except Exception:
        resolved = root.expanduser()
    normalized = str(resolved).replace("\\", "/").rstrip("/").casefold()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]


def detect_environment_label(root: Path) -> str:
    """フォルダ名と直下ファイル名から表示用の環境名を推定する。"""
    names = [root.name.casefold()]
    try:
        names.extend(path.name.casefold() for path in root.iterdir())
    except OSError:
        pass
    return "beatoraja"


def get_default_folder_profile(
    settings: AppSettings,
    root: Path,
    *,
    create: bool = True,
    migrate_legacy: bool = True,
) -> DefaultFolderProfile | None:
    key = environment_profile_key(root)
    for profile in settings.default_folder_profiles:
        if profile.profile_key == key:
            profile.root = str(root)
            profile.label = detect_environment_label(root)
            return profile
    if not create:
        return None
    folders: list[DefaultFolderEntry] = []
    if migrate_legacy and settings.default_folders:
        folders = settings.default_folders
        settings.default_folders = []
    profile = DefaultFolderProfile(
        profile_key=key,
        root=str(root),
        label=detect_environment_label(root),
        default_folders=folders,
    )
    settings.default_folder_profiles.append(profile)
    return profile


def active_default_folders(settings: AppSettings, root: Path | None) -> list[DefaultFolderEntry]:
    if root is None:
        return settings.default_folders
    profile = get_default_folder_profile(settings, root)
    return profile.default_folders if profile else []


def sync_default_folder_catalog(settings: AppSettings, default_json_path: Path) -> int:
    """default.jsonのBMSCF管理外ルートを設定へ取り込み、非表示項目も元の順序で保持する。"""
    if not default_json_path.exists():
        return 0
    data = safe_load_json(default_json_path, [])
    if not isinstance(data, list):
        raise ToolError(f"folder/default.json のルートが配列ではありません: {default_json_path}")
    managed_names = set(settings.managed_folder_names or []) | {MANAGED_ROOT_NAME}
    environment_root = default_json_path.parent.parent
    profile = get_default_folder_profile(settings, environment_root)
    catalog = list(profile.default_folders if profile else settings.default_folders)
    by_name: dict[str, list[DefaultFolderEntry]] = {}
    for entry in catalog:
        by_name.setdefault(entry.name, []).append(entry)
    used_count: dict[str, int] = {}
    imported = 0
    for index, payload in enumerate(data):
        if not isinstance(payload, dict):
            continue
        name = _entry_name(payload, index)
        if name in managed_names:
            continue
        ordinal = used_count.get(name, 0)
        used_count[name] = ordinal + 1
        candidates = by_name.get(name, [])
        if ordinal < len(candidates):
            entry = candidates[ordinal]
            entry.name = name
            entry.data = dict(payload)
        else:
            entry = DefaultFolderEntry(name=name, visible=True, data=dict(payload))
            catalog.append(entry)
            by_name.setdefault(name, []).append(entry)
            imported += 1
    if profile:
        profile.default_folders = catalog
    else:
        settings.default_folders = catalog
    return imported


def apply_default_folder_visibility(
    default_json_path: Path,
    settings: AppSettings,
) -> Path | None:
    """Immediately apply existing-root visibility without regenerating managed folders.

    Existing roots are reconstructed from the per-environment catalog, while the
    currently generated/managed roots are preserved exactly as they are in
    default.json.  Hidden entries therefore disappear from the file immediately
    and can later be restored from the saved catalog.
    """
    if default_json_path.exists():
        data = safe_load_json(default_json_path, [])
        if not isinstance(data, list):
            raise ToolError(f"folder/default.json のルートが配列ではありません: {default_json_path}")
    else:
        data = []

    # Capture roots added outside this tool before rewriting.  Matching catalog
    # entries keep their existing visible flag, so an already hidden entry is not
    # accidentally re-enabled.
    sync_default_folder_catalog(settings, default_json_path)
    managed_names = set(settings.managed_folder_names or []) | {MANAGED_ROOT_NAME}
    managed_roots = [
        dict(payload) for payload in data
        if isinstance(payload, dict) and _entry_name(payload, 0) in managed_names
    ]
    visible_existing = [
        dict(entry.data)
        for entry in active_default_folders(settings, default_json_path.parent.parent)
        if entry.visible
    ]
    backup = backup_file(default_json_path, "visibility") if default_json_path.exists() else None
    atomic_write_json(default_json_path, visible_existing + managed_roots)
    return backup


def _is_generated_folder_payload(value: Any) -> bool:
    """Return True for folder roots generated by this tool.

    Generated leaves and random-course stages reference the private bmscf_*
    support tables.  Detecting that marker lets v1.0.11 clean duplicates made
    by older public builds even when their managed-name list was lost.
    """
    if isinstance(value, dict):
        sql = value.get("sql")
        if isinstance(sql, str) and "bmscf_" in sql:
            return True
        return any(_is_generated_folder_payload(item) for item in value.values())
    if isinstance(value, list):
        return any(_is_generated_folder_payload(item) for item in value)
    return False


def _remove_managed_entries_from_catalog(
    settings: AppSettings,
    environment_root: Path,
    managed_names: set[str],
) -> None:
    """Remove generated roots accidentally captured as ordinary user folders.

    Older builds could import a generated root into the per-environment catalog
    before its name had been recorded as managed.  Once captured there, every
    later apply operation wrote the old payload and then appended the newly
    generated payload, causing duplicate roots to grow over time.
    """
    profile = get_default_folder_profile(settings, environment_root, create=False)
    if profile is not None:
        profile.default_folders = [
            entry for entry in profile.default_folders
            if entry.name not in managed_names and not _is_generated_folder_payload(entry.data)
        ]
    settings.default_folders = [
        entry for entry in settings.default_folders
        if entry.name not in managed_names and not _is_generated_folder_payload(entry.data)
    ]


def write_default_json_layout(
    default_json_path: Path,
    settings: AppSettings,
    generated_roots: list[dict[str, Any]],
) -> Path | None:
    # Register the current root names *before* catalog synchronization.  This
    # prevents the just-generated roots from being mistaken for existing user
    # folders, including on the first run after upgrading from an older build.
    new_names = [
        str(root.get("name") or "")
        for root in generated_roots
        if isinstance(root, dict) and str(root.get("name") or "")
    ]
    discovered_names: list[str] = []
    if default_json_path.exists():
        current_payload = safe_load_json(default_json_path, [])
        if isinstance(current_payload, list):
            discovered_names = [
                str(item.get("name") or "")
                for item in current_payload
                if isinstance(item, dict)
                and str(item.get("name") or "")
                and _is_generated_folder_payload(item)
            ]
    managed_names = (
        set(settings.managed_folder_names or [])
        | {MANAGED_ROOT_NAME}
        | set(new_names)
        | set(discovered_names)
    )
    settings.managed_folder_names = list(dict.fromkeys(
        [MANAGED_ROOT_NAME]
        + list(settings.managed_folder_names or [])
        + discovered_names
        + new_names
    ))

    sync_default_folder_catalog(settings, default_json_path)
    environment_root = default_json_path.parent.parent
    _remove_managed_entries_from_catalog(settings, environment_root, managed_names)

    backup = backup_file(default_json_path, "bmscf") if default_json_path.exists() else None
    visible_existing = [
        dict(entry.data)
        for entry in active_default_folders(settings, environment_root)
        if entry.visible and entry.name not in managed_names
    ]

    # Rebuild the managed section from scratch on every apply.  Existing
    # generated roots are never appended to; the same root names are replaced
    # by the latest definition, so repeated use from either window is idempotent.
    unique_generated: list[dict[str, Any]] = []
    emitted: set[str] = set()
    for root in generated_roots:
        if not isinstance(root, dict):
            continue
        name = str(root.get("name") or "")
        if not name or name in emitted:
            continue
        emitted.add(name)
        unique_generated.append(dict(root))

    atomic_write_json(default_json_path, visible_existing + unique_generated)
    return backup


@dataclass
class EnvironmentPaths:
    root: Path
    environment_profile_key: str
    environment_label: str
    config: Path
    song_db: Path
    table_dir: Path
    default_json: Path
    player_dir: Path
    score_db: Path | None
    scoredatalog_db: Path | None
    scorelog_db: Path | None
    player_name: str
    config_data: dict[str, Any]


@dataclass
class ViewDefinition:
    view_id: str
    base_view_id: str
    base_table_id: str
    base_table_name: str
    level_name: str
    combo_key: str
    combo_name: str
    cross_table_id: str
    preset_id: str
    preset_name: str
    charts: dict[str, ChartRef]
    preset: Preset | None = None
    preset_group_name: str = ""
    profile_id: str = ""
    profile_name: str = ""


@dataclass
class GenerationResult:
    view_count: int
    membership_count: int
    folder_json_path: Path
    backup_path: Path | None
    missing_same_level_count: int
    empty_view_count: int
    songdata_initial_backup_path: Path | None = None
    analysis_reclassified: int = 0
    analysis_analyzed: int = 0
    analysis_cached: int = 0
    analysis_missing: int = 0
    analysis_failed: int = 0
    condition_count: int = 0
    analysis_row_count: int = 0
    instant_chart_count: int = 0
    maker_folder_count: int = 0
    maker_chart_count: int = 0
    material_analyzed: int = 0
    material_cached: int = 0
    material_quick_cached: int = 0
    material_full_scans: int = 0
    material_failed: int = 0


@dataclass(frozen=True)
class ProgressUpdate:
    stage_key: str
    stage_label: str
    stage_current: int
    stage_total: int
    overall_current: float
    overall_total: float
    message: str = ""


ProgressCallback = Callable[[ProgressUpdate], None]
ItemProgressCallback = Callable[[int, int, str], None]
CancelCheck = Callable[[], bool]


class ProgressReporter:
    """Convert per-stage counters into one monotonic overall progress value."""

    def __init__(
        self,
        callback: ProgressCallback | None,
        stages: Iterable[tuple[str, str, float]],
    ) -> None:
        self.callback = callback
        self.stages = [(key, label, max(0.001, float(weight))) for key, label, weight in stages]
        self.by_key = {key: (index, label, weight) for index, (key, label, weight) in enumerate(self.stages)}
        self.overall_total = sum(weight for _key, _label, weight in self.stages) or 1.0
        self.last_overall = 0.0

    def update(
        self,
        stage_key: str,
        current: int,
        total: int,
        message: str = "",
    ) -> None:
        if self.callback is None or stage_key not in self.by_key:
            return
        index, label, weight = self.by_key[stage_key]
        before = sum(item[2] for item in self.stages[:index])
        safe_total = max(1, int(total))
        safe_current = min(max(0, int(current)), safe_total)
        overall = before + weight * safe_current / safe_total
        # Queue delivery may race at stage boundaries. Never move the bar backwards.
        overall = max(self.last_overall, min(overall, self.overall_total))
        self.last_overall = overall
        try:
            self.callback(ProgressUpdate(
                stage_key=stage_key,
                stage_label=label,
                stage_current=safe_current,
                stage_total=safe_total,
                overall_current=overall,
                overall_total=self.overall_total,
                message=message,
            ))
        except Exception:
            # Progress display must never abort generation.
            pass

    def start(self, stage_key: str, message: str = "") -> None:
        self.update(stage_key, 0, 1, message)

    def finish(self, stage_key: str, message: str = "") -> None:
        self.update(stage_key, 1, 1, message)


def safe_load_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        raise ToolError(f"JSONを読み込めません: {path}\n{exc}") from exc


def atomic_write_text(path: Path, text: str, encoding: str = "utf-8") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def atomic_write_json(path: Path, data: Any) -> None:
    text = json.dumps(data, ensure_ascii=False, indent=2)
    atomic_write_text(path, text + "\n")


def backup_file(path: Path, suffix_prefix: str = "backup") -> Path | None:
    if not path.exists():
        return None
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = path.with_name(f"{path.name}.{suffix_prefix}_{stamp}")
    shutil.copy2(path, backup)
    return backup


def load_settings(path: Path) -> AppSettings:
    data = safe_load_json(path, None)
    if not isinstance(data, dict):
        return AppSettings()
    return AppSettings.from_dict(data)


def save_settings(path: Path, settings: AppSettings) -> None:
    atomic_write_json(path, settings.to_dict())



def bundled_resource_path(filename: str) -> Path:
    """Return a bundled resource path in source and PyInstaller one-file builds."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / "resources" / filename


def difficulty_registry_path() -> Path:
    return bundled_resource_path(DIFFICULTY_REGISTRY_FILENAME)


def instant_density_profile_path() -> Path:
    return bundled_resource_path(INSTANT_DENSITY_PROFILE_FILENAME)


def validate_instant_density_profiles(profile: dict[str, Any]) -> dict[str, int]:
    if not isinstance(profile, dict):
        raise ToolError("即席密度順位基準のルートがオブジェクトではありません")
    if int(profile.get("schema_version") or 0) != 1:
        raise ToolError("即席密度順位基準のschema_versionが未対応です")
    bands = profile.get("bands")
    if not isinstance(bands, dict):
        raise ToolError("即席密度順位基準にbandsがありません")
    for band_id in ("sr", "sl", "st"):
        band = bands.get(band_id)
        if not isinstance(band, dict):
            raise ToolError(f"即席密度順位基準に{band_id}帯がありません")
        boundaries = band.get("density_boundaries")
        ratios = band.get("level_ratios")
        if not isinstance(boundaries, list) or len(boundaries) != 11:
            raise ToolError(f"{band_id}帯のdensity_boundariesは11個必要です")
        values = [float(x) for x in boundaries]
        if values != sorted(values) or len(set(values)) != 11:
            raise ToolError(f"{band_id}帯のdensity_boundariesは重複なしの昇順である必要があります")
        if not isinstance(ratios, list) or len(ratios) != 12:
            raise ToolError(f"{band_id}帯のlevel_ratiosは12個必要です")
        if any(float(x) < 0 for x in ratios) or sum(float(x) for x in ratios) <= 0:
            raise ToolError(f"{band_id}帯のlevel_ratiosが不正です")
        float(band.get("median_density"))
    return {
        "bands": 3,
        "boundaries": sum(len(bands[x]["density_boundaries"]) for x in ("sr", "sl", "st")),
        "sample_count": sum(int(bands[x].get("sample_count") or 0) for x in ("sr", "sl", "st")),
    }


def load_instant_density_profiles(path: Path | None = None) -> dict[str, Any]:
    source = path or instant_density_profile_path()
    profile = safe_load_json(source, None)
    if not isinstance(profile, dict):
        raise ToolError(f"即席密度順位基準を読み込めません: {source}")
    validate_instant_density_profiles(profile)
    return profile


def average_density(song: dict[str, Any] | None) -> float | None:
    if not song:
        return None
    try:
        notes = int(song.get("notes") or 0)
        length = int(song.get("length") or 0)
    except (TypeError, ValueError):
        return None
    if notes <= 0 or length <= 0:
        return None
    return notes * 1000.0 / length


def classify_instant_density(density: float, profile: dict[str, Any]) -> tuple[str, int]:
    validate_instant_density_profiles(profile)
    bands = profile["bands"]
    band_id = min(
        ("sr", "sl", "st"),
        key=lambda item: (abs(float(density) - float(bands[item]["median_density"])), ("sr", "sl", "st").index(item)),
    )
    boundaries = [float(x) for x in bands[band_id]["density_boundaries"]]
    return band_id, bisect.bisect_right(boundaries, float(density)) + 1


def instant_density_profile_summary(profile: dict[str, Any]) -> str:
    validate_instant_density_profiles(profile)
    parts = []
    for band_id in ("sr", "sl", "st"):
        band = profile["bands"][band_id]
        quality = "実測" if band.get("profile_quality") == "measured" else "暫定"
        parts.append(f"{band_id}: {int(band.get('sample_count') or 0):,}譜面 ({quality})")
    return f"{profile.get('profile_version', '?')} / " + " / ".join(parts)


def normalize_scale_text(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = text.replace("−", "-").replace("–", "-").replace("—", "-")
    return re.sub(r"\s+", "", text).casefold()


def validate_difficulty_scale_registry(registry: dict[str, Any]) -> dict[str, int]:
    if not isinstance(registry, dict):
        raise ToolError("難度基準レジストリがJSONオブジェクトではありません")
    if registry.get("schema_version") != 1:
        raise ToolError(f"未対応の難度基準レジストリ形式です: {registry.get('schema_version')}")
    axis = registry.get("canonical_axis")
    if not isinstance(axis, dict) or not isinstance(axis.get("points"), list):
        raise ToolError("難度基準レジストリにcanonical_axis.pointsがありません")
    positions: set[float] = set()
    for point in axis.get("points", []):
        if not isinstance(point, dict) or not isinstance(point.get("position"), (int, float)):
            raise ToolError("canonical_axis.pointsに不正な行があります")
        positions.add(float(point["position"]))
    source_ids: set[str] = set()
    for source in registry.get("sources", []):
        if not isinstance(source, dict) or not source.get("id"):
            raise ToolError("sourcesにIDのない項目があります")
        sid = str(source["id"])
        if sid in source_ids:
            raise ToolError(f"sourcesのIDが重複しています: {sid}")
        source_ids.add(sid)
    table_ids: set[str] = set()
    mapping_count = 0
    for table in registry.get("tables", []):
        if not isinstance(table, dict) or not table.get("id"):
            raise ToolError("tablesにIDのない項目があります")
        tid = str(table["id"])
        if tid in table_ids:
            raise ToolError(f"tablesのIDが重複しています: {tid}")
        table_ids.add(tid)
        seen_levels: set[str] = set()
        for mapping in table.get("mappings", []):
            if not isinstance(mapping, dict) or not mapping.get("level"):
                raise ToolError(f"{tid}: levelのないmappingがあります")
            key = normalize_scale_text(mapping["level"])
            if key in seen_levels:
                raise ToolError(f"{tid}: mappingが重複しています: {mapping['level']}")
            seen_levels.add(key)
            lo, hi = mapping.get("axis_min"), mapping.get("axis_max")
            if not isinstance(lo, (int, float)) or not isinstance(hi, (int, float)) or float(lo) > float(hi):
                raise ToolError(f"{tid}: 軸範囲が不正です: {mapping['level']}")
            if float(lo).is_integer() and float(lo) not in positions:
                raise ToolError(f"{tid}: 軸位置が未定義です: {lo}")
            if float(hi).is_integer() and float(hi) not in positions:
                raise ToolError(f"{tid}: 軸位置が未定義です: {hi}")
            for sid in mapping.get("source_ids", []):
                if sid not in source_ids:
                    raise ToolError(f"{tid}: 未定義の出典IDです: {sid}")
            mapping_count += 1
    family_ids: set[str] = set()
    for family in registry.get("same_number_families", []):
        if not isinstance(family, dict) or not family.get("id"):
            raise ToolError("same_number_familiesにIDのない項目があります")
        family_id = str(family["id"])
        if family_id in family_ids:
            raise ToolError(f"same_number_familiesのIDが重複しています: {family_id}")
        family_ids.add(family_id)
        canonical_table_id = str(family.get("canonical_table_id") or "")
        if canonical_table_id and canonical_table_id not in table_ids:
            raise ToolError(
                f"same_number_familiesの基準表が未定義です: {family_id} -> {canonical_table_id}"
            )
        if not isinstance(family.get("level_prefixes", []), list):
            raise ToolError(f"same_number_families.level_prefixesが不正です: {family_id}")
        if not isinstance(family.get("table_terms", []), list):
            raise ToolError(f"same_number_families.table_termsが不正です: {family_id}")
    for table in registry.get("tables", []):
        family_id = str(table.get("same_number_family") or "") if isinstance(table, dict) else ""
        if family_id and family_id not in family_ids:
            raise ToolError(f"tables.same_number_familyが未定義です: {table.get('id')} -> {family_id}")
    return {
        "table_count": len(table_ids),
        "source_count": len(source_ids),
        "mapping_count": mapping_count,
        "axis_point_count": len(positions),
        "same_number_family_count": len(family_ids),
    }


def load_difficulty_scale_registry(path: Path | None = None) -> dict[str, Any]:
    target = path or difficulty_registry_path()
    data = safe_load_json(target, None)
    if not isinstance(data, dict):
        raise ToolError(f"難度基準レジストリを読み込めません: {target}")
    validate_difficulty_scale_registry(data)
    return data


def difficulty_registry_sources(registry: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(source.get("id")): source
        for source in registry.get("sources", [])
        if isinstance(source, dict) and source.get("id")
    }


def difficulty_axis_labels(registry: dict[str, Any]) -> dict[float, str]:
    result: dict[float, str] = {}
    for point in registry.get("canonical_axis", {}).get("points", []):
        if isinstance(point, dict) and isinstance(point.get("position"), (int, float)):
            result[float(point["position"])] = str(point.get("label") or point["position"])
    return result


def match_difficulty_scale(
    table: TableInfo,
    registry: dict[str, Any],
) -> dict[str, Any] | None:
    """Match a loaded table to the bundled registry using URL, symbol, name and levels."""
    url = normalize_scale_text(table.url)
    tag = normalize_scale_text(table.tag)
    name = normalize_scale_text(table.name)
    level_keys = {normalize_scale_text(x) for x in table.levels}
    best: tuple[int, dict[str, Any] | None] = (0, None)
    for entry in registry.get("tables", []):
        if not isinstance(entry, dict):
            continue
        score = 0
        for pattern in entry.get("url_patterns", []):
            ptn = normalize_scale_text(pattern)
            if ptn and ptn in url:
                score = max(score, 100 + len(ptn))
        symbols = {normalize_scale_text(x) for x in entry.get("symbols", [])}
        if tag and tag in symbols:
            score += 45
        aliases = [entry.get("name", ""), *entry.get("aliases", [])]
        alias_keys = {normalize_scale_text(x) for x in aliases if x}
        if name and name in alias_keys:
            score += 35
        elif name and any(x and (x in name or name in x) for x in alias_keys):
            score += 18
        known_levels = {
            normalize_scale_text(x.get("level"))
            for x in entry.get("mappings", []) if isinstance(x, dict)
        }
        known_levels.update(normalize_scale_text(x) for x in entry.get("level_order", []))
        overlap = len(level_keys & known_levels)
        score += min(20, overlap * 2)
        if score > best[0]:
            best = (score, entry)
    return best[1] if best[0] >= 20 else None


def resolve_difficulty_level(
    entry: dict[str, Any],
    level_name: str,
    registry: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    raw = normalize_scale_text(level_name)
    candidates = {raw}
    if re.fullmatch(r"-?\d+(?:\.\d+)?[+?-]?", raw):
        candidates.update(normalize_scale_text(str(symbol) + str(level_name)) for symbol in entry.get("symbols", []))
    for mapping in entry.get("mappings", []):
        if isinstance(mapping, dict) and normalize_scale_text(mapping.get("level")) in candidates:
            result = dict(mapping)
            if registry is not None:
                labels = difficulty_axis_labels(registry)
                lo, hi = float(result["axis_min"]), float(result["axis_max"])
                result["axis_min_label"] = labels.get(lo, str(result["axis_min"]))
                result["axis_max_label"] = labels.get(hi, str(result["axis_max"]))
            return result
    return None


def resolve_table_level_range(
    table: TableInfo,
    level_name: str,
    registry: dict[str, Any],
) -> dict[str, Any] | None:
    entry = match_difficulty_scale(table, registry)
    if entry is None:
        return None
    mapping = resolve_difficulty_level(entry, level_name, registry)
    if mapping is None:
        return None
    result = dict(mapping)
    result["table_id"] = entry.get("id")
    result["table_name"] = entry.get("name")
    return result


def difficulty_same_number_family(
    table: TableInfo,
    level_name: str,
    registry: dict[str, Any] | None = None,
) -> str | None:
    """Resolve the same-number scale family for one level.

    The bundled registry separates the lower sl-numbered family from the
    upper st-numbered family.  This is intentionally distinct from the common
    difficulty axis: sl12 and st0 share a boundary on that axis, but they are
    different same-number series and must not be merged merely because both
    end in a numeric value.
    """
    registry = registry or load_difficulty_scale_registry()
    rules = [x for x in registry.get("same_number_families", []) if isinstance(x, dict)]
    level_key = normalize_scale_text(level_name)

    # Prefer an explicit level prefix.  This is essential for generated tables
    # that contain both sl0..12 and st0..12 under one TableInfo.
    for rule in rules:
        family_id = str(rule.get("id") or "")
        for raw_prefix in rule.get("level_prefixes", []):
            prefix = normalize_scale_text(raw_prefix)
            if not prefix or not level_key.startswith(prefix):
                continue
            tail = level_key[len(prefix):]
            if re.match(r"^-?\d", tail):
                return family_id

    matched = match_difficulty_scale(table, registry)
    if matched is not None:
        family_id = str(matched.get("same_number_family") or "")
        if family_id:
            return family_id
        matched_id = str(matched.get("id") or "")
        for rule in rules:
            if str(rule.get("canonical_table_id") or "") == matched_id:
                return str(rule.get("id") or "") or None

    tag_key = normalize_scale_text(table.tag)
    name_key = normalize_scale_text(table.name)
    for rule in rules:
        family_id = str(rule.get("id") or "")
        terms = [normalize_scale_text(x) for x in rule.get("table_terms", [])]
        for term in terms:
            if not term:
                continue
            # Short symbols such as sl/st/dl are exact-tag matches only, so
            # "st" does not accidentally match names such as Starlight.
            if tag_key == term:
                return family_id
            if len(term) >= 3 and term in name_key:
                return family_id
    return None


def difficulty_scale_summary(entry: dict[str, Any]) -> str:
    level_count = len(entry.get("level_order", []))
    mapping_count = len(entry.get("mappings", []))
    mapped = f"{mapping_count}/{level_count}" if level_count else str(mapping_count)
    statuses = {str(x.get("confidence") or "unknown") for x in entry.get("mappings", []) if isinstance(x, dict)}
    confidence = ", ".join(sorted(statuses)) if statuses else "未換算"
    return f"換算 {mapped} / 確度 {confidence}"


def table_id_for_url(url: str) -> str:
    return hashlib.sha256((url or "").strip().encode("utf-8")).hexdigest()[:20]


def bmt_filename_for_url(url: str) -> str:
    return hashlib.sha256((url or "").strip().encode("utf-8")).hexdigest() + ".bmt"


def resolve_path(root: Path, value: str | None, default: str) -> Path:
    raw = (value or default).strip()
    p = Path(raw)
    return p if p.is_absolute() else root / p


def list_player_names(root: Path) -> list[str]:
    """List player profile directories for the selected beatoraja root."""
    root = root.expanduser().resolve()
    config = root / "config_sys.json"
    if not config.exists():
        legacy = root / "config.json"
        if legacy.exists():
            config = legacy
        else:
            return ["player1"]
    data = safe_load_json(config, {})
    if not isinstance(data, dict):
        return ["player1"]
    player_dir = resolve_path(root, data.get("playerpath"), "player")
    names: list[str] = []
    try:
        names = sorted(
            [item.name for item in player_dir.iterdir() if item.is_dir()],
            key=str.casefold,
        )
    except OSError:
        names = []
    configured = str(data.get("playername") or "player1").strip() or "player1"
    result = [configured] + [name for name in names if name != configured]
    if "player1" not in result:
        result.append("player1")
    return list(dict.fromkeys(result))


def detect_environment(root: Path, player_name_override: str | None = None) -> EnvironmentPaths:
    root = root.expanduser().resolve()
    config = root / "config_sys.json"
    if not config.exists():
        legacy = root / "config.json"
        if legacy.exists():
            config = legacy
        else:
            raise ToolError(f"config_sys.json が見つかりません: {root}")
    config_data = safe_load_json(config, {})
    if not isinstance(config_data, dict):
        raise ToolError(f"設定ファイルの形式が不正です: {config}")
    song_db = resolve_path(root, config_data.get("songpath"), "songdata.db")
    table_dir = resolve_path(root, config_data.get("tablepath"), "table")
    player_dir = resolve_path(root, config_data.get("playerpath"), "player")
    player_name = str(player_name_override or config_data.get("playername") or "player1").strip() or "player1"
    # Honour the selected player exactly. Falling back to player1 here would mix
    # another player's score/BP/history into the generated folders.
    selected_player_dir = player_dir / player_name
    score_candidate = selected_player_dir / "score.db"
    score_db = score_candidate if score_candidate.exists() else None
    scoredatalog_candidate = selected_player_dir / "scoredatalog.db"
    scoredatalog_db = scoredatalog_candidate if scoredatalog_candidate.exists() else None
    scorelog_candidate = selected_player_dir / "scorelog.db"
    scorelog_db = scorelog_candidate if scorelog_candidate.exists() else None
    default_json = root / "folder" / "default.json"
    return EnvironmentPaths(
        root=root,
        environment_profile_key=environment_profile_key(root),
        environment_label=detect_environment_label(root),
        config=config,
        song_db=song_db,
        table_dir=table_dir,
        default_json=default_json,
        player_dir=player_dir,
        score_db=score_db,
        scoredatalog_db=scoredatalog_db,
        scorelog_db=scorelog_db,
        player_name=player_name,
        config_data=config_data,
    )


def common_root_candidates() -> list[Path]:
    candidates: list[Path] = []
    env = os.environ.get("LR2ORAJA_ROOT")
    if env:
        candidates.append(Path(env))
    candidates.extend([
        Path.cwd(),
        Path.cwd().parent,
    ])
    seen: set[str] = set()
    result: list[Path] = []
    for p in candidates:
        key = str(p).lower()
        if key in seen:
            continue
        seen.add(key)
        if (p / "config_sys.json").exists() or (p / "config.json").exists():
            result.append(p)
    return result


def register_table_url(config_path: Path, url: str) -> tuple[bool, Path | None]:
    url = (url or "").strip()
    if not url:
        raise ToolError("URLが空です")
    data = safe_load_json(config_path, {})
    if not isinstance(data, dict):
        raise ToolError("beatoraja設定ファイルがJSONオブジェクトではありません")
    urls = data.get("tableURL")
    if not isinstance(urls, list):
        urls = []
    if url in urls:
        return False, None
    backup = backup_file(config_path, "bmscf")
    urls.append(url)
    data["tableURL"] = urls
    atomic_write_json(config_path, data)
    return True, backup


def remove_table_url(config_path: Path, url: str) -> tuple[bool, Path | None]:
    data = safe_load_json(config_path, {})
    urls = data.get("tableURL")
    if not isinstance(urls, list) or url not in urls:
        return False, None
    backup = backup_file(config_path, "bmscf")
    data["tableURL"] = [x for x in urls if x != url]
    atomic_write_json(config_path, data)
    return True, backup


def merge_chart_maps(target: dict[str, ChartRef], source: dict[str, ChartRef]) -> None:
    for key, chart in source.items():
        if not key:
            continue
        if key not in target:
            target[key] = chart.normalized()
            continue
        old = target[key]
        if not old.sha256 and chart.sha256:
            old.sha256 = chart.sha256.lower()
        if not old.md5 and chart.md5:
            old.md5 = chart.md5.lower()
        if not old.title and chart.title:
            old.title = chart.title
        if not old.artist and chart.artist:
            old.artist = chart.artist


def chart_from_record(record: dict[str, Any]) -> ChartRef:
    def first(keys: Iterable[str]) -> str:
        for key in keys:
            value = record.get(key)
            if value not in (None, ""):
                return str(value)
        return ""
    return ChartRef(
        sha256=first(["sha256", "SHA256", "sha", "hash_sha256"]),
        md5=first(["md5", "MD5", "hash", "lr2_bmsid"]),
        title=first(["title", "Title", "name", "song"]),
        artist=first(["artist", "Artist"]),
    ).normalized()


def parse_bmt(path: Path, url_hint: str = "") -> TableInfo:
    try:
        with gzip.open(path, "rt", encoding="utf-8-sig") as f:
            data = json.load(f)
    except Exception as exc:
        raise ToolError(f"難易度表キャッシュを読み込めません: {path}\n{exc}") from exc
    if not isinstance(data, dict):
        raise ToolError(f"難易度表キャッシュの形式が不正です: {path}")
    url = str(data.get("url") or url_hint or "")
    table = TableInfo(
        table_id=table_id_for_url(url or path.name),
        url=url,
        name=str(data.get("name") or path.stem),
        tag=str(data.get("tag") or ""),
        source="beatoraja",
    )
    folders = data.get("folder") or data.get("folders") or []
    if not isinstance(folders, list):
        folders = []
    for folder in folders:
        if not isinstance(folder, dict):
            continue
        level_name = str(folder.get("name") or "").strip()
        if not level_name:
            continue
        songs = folder.get("songs") or folder.get("song") or []
        if not isinstance(songs, list):
            continue
        charts: dict[str, ChartRef] = {}
        for record in songs:
            if not isinstance(record, dict):
                continue
            chart = chart_from_record(record)
            key = chart.preferred_key()
            if key:
                merge_chart_maps(charts, {key: chart})
        if charts:
            table.levels[level_name] = TableLevel(level_name, charts)
    if not table.levels:
        table.error = "有効なレベル・譜面がありません"
    return table


def fetch_text(url: str, timeout: int = 30) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": f"{APP_NAME}/{APP_VERSION} Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as res:
        raw = res.read()
    for enc in ("utf-8-sig", "utf-8", "cp932"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def fetch_json(url: str, timeout: int = 30) -> Any:
    text = fetch_text(url, timeout=timeout).strip()
    if not text.startswith("{") and not text.startswith("["):
        match = re.search(r"\((.*)\)\s*;?\s*$", text, re.S)
        if match:
            text = match.group(1)
    return json.loads(text)


def guess_header_url(table_url: str) -> str:
    table_url = table_url.strip()
    if table_url.lower().endswith(".json"):
        return table_url
    if table_url.lower().endswith((".html", ".htm", ".php")):
        return urljoin(table_url, "header.json")
    return table_url.rstrip("/") + "/header.json"


def parse_remote_table(table_url: str, header_url: str = "", timeout: int = 30) -> TableInfo:
    table_url = table_url.strip()
    header_url = (header_url or guess_header_url(table_url)).strip()
    try:
        header = fetch_json(header_url, timeout=timeout)
    except Exception as first_exc:
        # Some table pages expose bmstable/header links in HTML.
        try:
            html_text = fetch_text(table_url, timeout=timeout)
            patterns = [
                r'<meta[^>]+name=["\']bmstable["\'][^>]+content=["\']([^"\']+)',
                r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']bmstable["\']',
                r'["\']([^"\']*(?:header|head)[^"\']*\.json)["\']',
            ]
            found = ""
            for pattern in patterns:
                match = re.search(pattern, html_text, re.I)
                if match:
                    found = match.group(1)
                    break
            if not found:
                raise first_exc
            header_url = urljoin(table_url, found)
            header = fetch_json(header_url, timeout=timeout)
        except Exception as exc:
            raise ToolError(
                "難易度表ヘッダーを取得できませんでした。\n"
                f"表URL: {table_url}\n試行ヘッダー: {header_url}\n{exc}"
            ) from exc
    if not isinstance(header, dict):
        raise ToolError("難易度表ヘッダーがJSONオブジェクトではありません")
    data_url = header.get("data_url") or header.get("data") or header.get("body_url")
    if isinstance(data_url, list):
        data_urls = [urljoin(header_url, str(x)) for x in data_url]
    elif data_url:
        data_urls = [urljoin(header_url, str(data_url))]
    else:
        raise ToolError("難易度表ヘッダーに data_url がありません")
    records: list[dict[str, Any]] = []
    for one_url in data_urls:
        body = fetch_json(one_url, timeout=timeout)
        if isinstance(body, dict):
            for key in ("data", "songs", "body", "elements"):
                if isinstance(body.get(key), list):
                    body = body[key]
                    break
        if isinstance(body, list):
            records.extend(x for x in body if isinstance(x, dict))
    name = str(header.get("name") or header.get("title") or table_url)
    tag = str(header.get("symbol") or header.get("tag") or "")
    table = TableInfo(
        table_id=table_id_for_url(table_url),
        url=table_url,
        name=name,
        tag=tag,
        source="tool",
        header_url=header_url,
    )
    level_order = header.get("level_order") or header.get("level_description") or []
    if not isinstance(level_order, list):
        level_order = []
    grouped: dict[str, dict[str, ChartRef]] = {}
    for record in records:
        raw_level = record.get("level")
        if raw_level is None:
            continue
        level = str(raw_level)
        display = f"{tag}{level}" if tag and not level.startswith(tag) else level
        chart = chart_from_record(record)
        key = chart.preferred_key()
        if not key:
            continue
        grouped.setdefault(display, {})
        merge_chart_maps(grouped[display], {key: chart})
    ordered_names: list[str] = []
    for level in level_order:
        display = f"{tag}{level}" if tag and not str(level).startswith(tag) else str(level)
        if display in grouped:
            ordered_names.append(display)
    ordered_names.extend(name for name in grouped if name not in ordered_names)
    for level_name in ordered_names:
        table.levels[level_name] = TableLevel(level_name, grouped[level_name])
    if not table.levels:
        table.error = "有効なレベル・譜面がありません"
    return table


def table_to_cache_dict(table: TableInfo) -> dict[str, Any]:
    return {
        "url": table.url,
        "name": table.name,
        "tag": table.tag,
        "source": table.source,
        "header_url": table.header_url,
        "levels": [
            {
                "name": level.name,
                "charts": [asdict(chart) for chart in level.charts.values()],
            }
            for level in table.levels.values()
        ],
    }


def table_from_cache_dict(data: dict[str, Any]) -> TableInfo:
    url = str(data.get("url") or "")
    table = TableInfo(
        table_id=table_id_for_url(url),
        url=url,
        name=str(data.get("name") or url),
        tag=str(data.get("tag") or ""),
        source=str(data.get("source") or "tool"),
        header_url=str(data.get("header_url") or ""),
    )
    for level_data in data.get("levels", []):
        if not isinstance(level_data, dict):
            continue
        level_name = str(level_data.get("name") or "")
        charts: dict[str, ChartRef] = {}
        for c in level_data.get("charts", []):
            if not isinstance(c, dict):
                continue
            chart = ChartRef(**{k: c.get(k, "") for k in ("sha256", "md5", "title", "artist")}).normalized()
            if chart.preferred_key():
                merge_chart_maps(charts, {chart.preferred_key(): chart})
        if level_name and charts:
            table.levels[level_name] = TableLevel(level_name, charts)
    return table


def load_tool_cache(cache_dir: Path, url: str) -> TableInfo | None:
    path = cache_dir / (hashlib.sha256(url.encode("utf-8")).hexdigest() + ".json")
    data = safe_load_json(path, None)
    if isinstance(data, dict):
        return table_from_cache_dict(data)
    return None


def save_tool_cache(cache_dir: Path, table: TableInfo) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / (hashlib.sha256(table.url.encode("utf-8")).hexdigest() + ".json")
    atomic_write_json(path, table_to_cache_dict(table))
    return path


def load_tables(
    env: EnvironmentPaths,
    app_tables: list[AppTableURL],
    cache_dir: Path,
    fetch_missing: bool = False,
) -> list[TableInfo]:
    config_urls = env.config_data.get("tableURL") or []
    if not isinstance(config_urls, list):
        config_urls = []
    app_by_url = {t.url: t for t in app_tables if t.url}
    urls: list[str] = []
    for url in list(config_urls) + list(app_by_url.keys()):
        url = str(url).strip()
        if url and url not in urls:
            urls.append(url)
    tables: list[TableInfo] = []
    for url in urls:
        bmt_path = env.table_dir / bmt_filename_for_url(url)
        table: TableInfo | None = None
        if bmt_path.exists():
            try:
                table = parse_bmt(bmt_path, url)
                table.source = "beatoraja"
            except Exception as exc:
                table = TableInfo(
                    table_id=table_id_for_url(url),
                    url=url,
                    name=url,
                    source="beatoraja",
                    error=str(exc),
                )
        if table is None:
            table = load_tool_cache(cache_dir, url)
        if table is None and fetch_missing:
            app_rec = app_by_url.get(url)
            header_override = app_rec.header_url if app_rec else ""
            try:
                table = parse_remote_table(url, header_override)
                save_tool_cache(cache_dir, table)
            except Exception as exc:
                table = TableInfo(
                    table_id=table_id_for_url(url),
                    url=url,
                    name=url,
                    source="tool" if url in app_by_url else "beatoraja",
                    error=str(exc),
                )
        if table is None:
            table = TableInfo(
                table_id=table_id_for_url(url),
                url=url,
                name=url,
                source="tool" if url in app_by_url else "beatoraja",
                error="キャッシュ未取得",
            )
        if url in app_by_url and table.source != "beatoraja":
            table.source = "tool"
        tables.append(table)
    return tables


def read_song_rows(
    song_db: Path,
    progress: ItemProgressCallback | None = None,
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    if not song_db.exists():
        raise ToolError(f"songdata.db が見つかりません: {song_db}")
    by_sha: dict[str, dict[str, Any]] = {}
    by_md5: dict[str, dict[str, Any]] = {}
    try:
        con = sqlite3.connect(f"file:{song_db.as_posix()}?mode=ro", uri=True, timeout=10)
        con.row_factory = sqlite3.Row
        if progress:
            progress(0, 1, "songdata.dbの件数を確認中")
        total = int(con.execute("SELECT count(*) FROM song").fetchone()[0])
        cursor = con.execute(
            """
            SELECT lower(sha256) AS sha256, lower(md5) AS md5,
                   title, artist, level, notes, minbpm, maxbpm,
                   difficulty, mode, favorite, length, path
            FROM song
            """
        )
        processed = 0
        batch_size = 5000
        while True:
            rows = cursor.fetchmany(batch_size)
            if not rows:
                break
            for row in rows:
                d = dict(row)
                sha = d.get("sha256") or ""
                md5 = d.get("md5") or ""
                if sha:
                    by_sha[sha] = d
                if md5:
                    by_md5[md5] = d
            processed += len(rows)
            if progress:
                progress(processed, max(total, 1), f"楽曲DB {processed:,}/{total:,}譜面")
        con.close()
        if progress:
            progress(max(total, processed), max(total, processed, 1), f"楽曲DB {processed:,}譜面を読込済み")
    except sqlite3.Error as exc:
        raise ToolError(f"songdata.dbを読み込めません: {exc}") from exc
    return by_sha, by_md5


def unique_song_rows(
    songs_by_sha: dict[str, dict[str, Any]],
    songs_by_md5: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for song in list(songs_by_sha.values()) + list(songs_by_md5.values()):
        key = str(song.get("path") or song.get("sha256") or song.get("md5") or "")
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(song)
    return result


def chart_ref_from_song(song: dict[str, Any]) -> ChartRef:
    return ChartRef(
        sha256=str(song.get("sha256") or "").lower(),
        md5=str(song.get("md5") or "").lower(),
        title=str(song.get("title") or ""),
        artist=str(song.get("artist") or ""),
    )


def build_instant_density_table(
    tables: list[TableInfo],
    songs_by_sha: dict[str, dict[str, Any]],
    songs_by_md5: dict[str, dict[str, Any]],
    profile: dict[str, Any],
) -> TableInfo:
    validate_instant_density_profiles(profile)
    known_keys: set[str] = set()
    for table in tables:
        if table.error or table.table_id in {INSTANT_TABLE_ID, MAKER_TABLE_ID}:
            continue
        known_keys.update(table.all_charts().keys())
    level_names = [f"{band}帯・密度順位{rank}" for band in ("sr", "sl", "st") for rank in range(1, 13)]
    levels = {name: TableLevel(name=name) for name in level_names}
    levels["密度解析不能"] = TableLevel(name="密度解析不能")
    for song in unique_song_rows(songs_by_sha, songs_by_md5):
        if int(song.get("mode") or 0) != 7:
            continue
        chart = chart_ref_from_song(song).normalized()
        key = chart.preferred_key()
        if not key or key in known_keys:
            continue
        density = average_density(song)
        if density is None:
            levels["密度解析不能"].charts[key] = chart
            continue
        band_id, rank = classify_instant_density(density, profile)
        levels[f"{band_id}帯・密度順位{rank}"].charts[key] = chart
    return TableInfo(
        table_id=INSTANT_TABLE_ID,
        url="bmscf://instant-density",
        name="表外差分即席難易度表",
        tag="密度順位",
        source="virtual",
        levels=levels,
    )


def read_scores(
    score_db: Path | None,
    progress: ItemProgressCallback | None = None,
) -> dict[str, dict[str, Any]]:
    if score_db is None or not score_db.exists():
        if progress:
            progress(1, 1, "score.dbなし")
        return {}
    result: dict[str, dict[str, Any]] = {}
    try:
        con = sqlite3.connect(f"file:{score_db.as_posix()}?mode=ro", uri=True, timeout=10)
        con.row_factory = sqlite3.Row
        total = int(con.execute("SELECT count(*) FROM score").fetchone()[0])
        cursor = con.execute(
            """
            SELECT lower(sha256) AS sha256, clear, minbp, playcount, date,
                   notes, epg, lpg, egr, lgr
            FROM score
            """
        )
        processed = 0
        batch_size = 2000
        while True:
            rows = cursor.fetchmany(batch_size)
            if not rows:
                break
            for row in rows:
                d = dict(row)
                sha = d.get("sha256") or ""
                if not sha:
                    continue
                exscore = int(d.get("epg") or 0) * 2 + int(d.get("lpg") or 0) * 2 + int(d.get("egr") or 0) + int(d.get("lgr") or 0)
                notes = int(d.get("notes") or 0)
                d["score_rate"] = (exscore * 50.0 / notes) if notes > 0 else 0.0
                old = result.get(sha)
                if old is None:
                    result[sha] = d
                else:
                    old["clear"] = max(int(old.get("clear") or 0), int(d.get("clear") or 0))
                    bp_values = [x for x in (old.get("minbp"), d.get("minbp")) if isinstance(x, int) and x < 2_000_000_000]
                    old["minbp"] = min(bp_values) if bp_values else None
                    old["playcount"] = max(int(old.get("playcount") or 0), int(d.get("playcount") or 0))
                    old["date"] = max(int(old.get("date") or 0), int(d.get("date") or 0))
                    old["score_rate"] = max(float(old.get("score_rate") or 0.0), float(d.get("score_rate") or 0.0))
            processed += len(rows)
            if progress:
                progress(processed, max(total, 1), f"プレイ記録 {processed:,}/{total:,}件")
        con.close()
        if progress:
            progress(max(total, processed), max(total, processed, 1), f"プレイ記録 {processed:,}件を読込済み")
    except sqlite3.Error as exc:
        raise ToolError(f"score.dbを読み込めません: {exc}") from exc
    return result


def score_rate_to_rank(score_rate: float, played: bool = True) -> str:
    if not played:
        return "NO PLAY"
    rate = max(0.0, min(100.0, float(score_rate)))
    if rate >= 100.0 - 1e-9:
        return "MAX"
    if rate >= (8.0 / 9.0) * 100.0:
        return "AAA"
    if rate >= (7.0 / 9.0) * 100.0:
        return "AA"
    if rate >= (6.0 / 9.0) * 100.0:
        return "A"
    if rate >= (5.0 / 9.0) * 100.0:
        return "B"
    if rate >= (4.0 / 9.0) * 100.0:
        return "C"
    if rate >= (3.0 / 9.0) * 100.0:
        return "D"
    if rate >= (2.0 / 9.0) * 100.0:
        return "E"
    return "F"


def _read_bms_text(path: Path) -> str:
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "cp932", "shift_jis", "utf-8"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("cp932", errors="replace")


def _bms_key_channel(channel: str) -> bool:
    channel = channel.upper()
    if len(channel) != 2:
        return False
    if channel[0] in {"1", "2"} and channel[1] in "12345789":
        return True
    if channel[0] in {"5", "6"} and channel[1] in "12345789":
        return True
    return False


def _bms_long_channel(channel: str) -> bool:
    channel = channel.upper()
    return len(channel) == 2 and channel[0] in {"5", "6"} and channel[1] in "12345789"


def analyze_bms_file(path: Path) -> dict[str, Any]:
    """Analyze keyboard-onset rhythm. Scratch, mines and LN ends are excluded."""
    text = _read_bms_text(path)
    base_bpm = 0.0
    bpm_defs: dict[str, float] = {}
    lnobj = ""
    measure_lengths: dict[int, Fraction] = {}
    data_lines: list[tuple[int, str, str]] = []

    data_re = re.compile(r"^#(\d{3})([0-9A-Za-z]{2}):(.+)$")
    bpm_ext_re = re.compile(r"^#BPM([0-9A-Za-z]{2})\s+(.+)$", re.IGNORECASE)
    bpm_re = re.compile(r"^#BPM\s+(.+)$", re.IGNORECASE)
    lnobj_re = re.compile(r"^#LNOBJ\s+([0-9A-Za-z]{2})", re.IGNORECASE)

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line.startswith("#"):
            continue
        m = data_re.match(line)
        if m:
            measure = int(m.group(1))
            channel = m.group(2).upper()
            value = m.group(3).strip()
            if channel == "02":
                try:
                    measure_lengths[measure] = Fraction(str(float(value)))
                except (ValueError, ZeroDivisionError):
                    pass
            else:
                data_lines.append((measure, channel, value))
            continue
        m = bpm_ext_re.match(line)
        if m:
            try:
                bpm_defs[m.group(1).upper()] = float(m.group(2).strip())
            except ValueError:
                pass
            continue
        m = bpm_re.match(line)
        if m:
            try:
                base_bpm = float(m.group(1).strip())
            except ValueError:
                pass
            continue
        m = lnobj_re.match(line)
        if m:
            lnobj = m.group(1).upper()

    max_measure = max([m for m, _, _ in data_lines] + list(measure_lengths.keys()) + [0])
    measure_starts: list[Fraction] = [Fraction(0)] * (max_measure + 2)
    for measure in range(max_measure + 1):
        measure_starts[measure + 1] = measure_starts[measure] + Fraction(4) * measure_lengths.get(measure, Fraction(1))

    def positions(measure: int, value: str) -> list[tuple[Fraction, str]]:
        if len(value) < 2:
            return []
        if len(value) % 2:
            value = value[:-1]
        count = len(value) // 2
        if count <= 0:
            return []
        measure_beats = Fraction(4) * measure_lengths.get(measure, Fraction(1))
        result: list[tuple[Fraction, str]] = []
        for i in range(count):
            token = value[i * 2:i * 2 + 2].upper()
            if token == "00":
                continue
            beat = measure_starts[measure] + measure_beats * Fraction(i, count)
            result.append((beat, token))
        return result

    normal_onsets: list[Fraction] = []
    long_by_lane: dict[str, list[Fraction]] = {}
    bpm_events: list[tuple[Fraction, float]] = []
    for measure, channel, value in data_lines:
        events = positions(measure, value)
        if channel == "03":
            for beat, token in events:
                try:
                    bpm_events.append((beat, float(int(token, 16))))
                except ValueError:
                    pass
            continue
        if channel == "08":
            for beat, token in events:
                bpm = bpm_defs.get(token)
                if bpm and bpm > 0:
                    bpm_events.append((beat, bpm))
            continue
        if not _bms_key_channel(channel):
            continue
        if _bms_long_channel(channel):
            long_by_lane.setdefault(channel, []).extend(beat for beat, _token in events)
        else:
            for beat, token in events:
                if lnobj and token == lnobj:
                    continue
                normal_onsets.append(beat)

    for lane_events in long_by_lane.values():
        for index, beat in enumerate(sorted(set(lane_events))):
            if index % 2 == 0:
                normal_onsets.append(beat)

    onsets = sorted(set(normal_onsets))
    if base_bpm <= 0:
        base_bpm = next((bpm for _beat, bpm in sorted(bpm_events) if bpm > 0), 120.0)
    bpm_events.sort(key=lambda item: item[0])
    bpm_beats = [float(beat) for beat, _bpm in bpm_events]
    bpm_values = [float(bpm) for _beat, bpm in bpm_events]

    def bpm_at(beat: Fraction) -> float:
        index = bisect.bisect_right(bpm_beats, float(beat)) - 1
        return bpm_values[index] if index >= 0 else base_bpm

    candidates: dict[int, float] = {
        8: 1.0 / 2.0,
        16: 1.0 / 4.0,
        32: 1.0 / 8.0,
        12: 1.0 / 3.0,
        24: 1.0 / 6.0,
        48: 1.0 / 12.0,
    }
    speed_factors = {8: 0.5, 16: 1.0, 32: 2.0, 12: 0.75, 24: 1.5, 48: 3.0}
    counts = {division: 0 for division in candidates}
    effective_samples: dict[int, list[float]] = {division: [] for division in candidates}
    for start, end in zip(onsets, onsets[1:]):
        delta = float(end - start)
        if delta <= 0 or delta > 0.55:
            continue
        division = min(candidates, key=lambda key: abs(delta - candidates[key]))
        target = candidates[division]
        if abs(delta - target) > max(1e-7, target * 0.06):
            continue
        counts[division] += 1
        effective_samples[division].append(bpm_at(start) * speed_factors[division])

    score16 = counts[8] + counts[16] + counts[32]
    score12 = counts[12] + counts[24] + counts[48]
    family = 16 if score16 >= score12 else 12
    family_divisions = [16, 8, 32] if family == 16 else [12, 24, 48]
    dominant = max(family_divisions, key=lambda div: (counts[div], -family_divisions.index(div)))
    samples: list[float] = []
    for division in family_divisions:
        samples.extend(effective_samples[division])
    if samples:
        effective_bpm = float(median(samples))
    else:
        dominant = 16 if family == 16 else 12
        effective_bpm = base_bpm * speed_factors[dominant]

    return {
        "rhythm_family": family,
        "dominant_division": dominant,
        "effective_bpm": round(effective_bpm, 4),
        "score16": score16,
        "score12": score12,
        "onset_count": len(onsets),
    }


def _analysis_connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=15)
    con.row_factory = sqlite3.Row
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS chart_analysis (
            sha256 TEXT PRIMARY KEY,
            file_path TEXT NOT NULL,
            file_mtime_ns INTEGER NOT NULL,
            file_size INTEGER NOT NULL,
            analysis_version INTEGER NOT NULL,
            rhythm_family INTEGER,
            dominant_division INTEGER,
            effective_bpm REAL,
            score16 INTEGER NOT NULL DEFAULT 0,
            score12 INTEGER NOT NULL DEFAULT 0,
            onset_count INTEGER NOT NULL DEFAULT 0,
            analyzed_at INTEGER NOT NULL,
            error TEXT NOT NULL DEFAULT ''
        )
        """
    )
    _ensure_attr_columns(con)
    return con


_ATTR_COLUMN_DEFS: list[tuple[str, str]] = (
    [(name, "INTEGER") for name in ATTR_INT_COLUMNS]
    + [(name, "REAL") for name in ATTR_NUMERIC_COLUMNS]
    + [("attr_conf", "REAL")]
)
_ATTR_RULE_COLUMN_DEFS: list[tuple[str, str]] = [
    ("attr_rule_pack_id", "TEXT"),
    ("attr_rule_version", "TEXT"),
    ("attr_rule_hash", "TEXT"),
]


def _ensure_attr_columns(con: sqlite3.Connection) -> None:
    """既存の chart_analysis テーブルへ属性解析・分類ルール情報を追加する。"""
    existing = {row[1] for row in con.execute("PRAGMA table_info(chart_analysis)")}
    for name, sql_type in _ATTR_COLUMN_DEFS + _ATTR_RULE_COLUMN_DEFS:
        if name not in existing:
            con.execute(f"ALTER TABLE chart_analysis ADD COLUMN {name} {sql_type}")


def read_analysis_rows(analysis_db: Path | None) -> dict[str, dict[str, Any]]:
    if analysis_db is None or not analysis_db.exists():
        return {}
    con = _analysis_connect(analysis_db)
    rows = con.execute("SELECT * FROM chart_analysis").fetchall()
    con.close()
    result = {str(row["sha256"]).lower(): dict(row) for row in rows if row["sha256"]}
    for row in result.values():
        attr, attr_sub = migrate_legacy_attr(row.get("attr"), row.get("attr_sub"))
        row["attr"], row["attr_sub"] = attr, attr_sub
        row["attr2"], _ = migrate_legacy_attr(row.get("attr2"))
    for sha, manual_attr in read_attr_overrides(analysis_db).items():
        row = result.get(sha)
        if row is not None:
            auto_attr = int(row.get("attr") or 0)
            auto_attr2 = int(row.get("attr2") or 0)
            row["attr_auto"] = auto_attr
            row["attr2_auto"] = auto_attr2
            row["attr_conf_auto"] = row.get("attr_conf")
            manual_attr, manual_sub = migrate_legacy_attr(manual_attr)
            row["attr"] = manual_attr
            if manual_sub is not None:
                row["attr_sub"] = manual_sub
            # 手動属性と第二属性が重複しないよう、自動第一属性を副属性へ退避。
            if manual_attr != auto_attr and auto_attr != ATTR_CODE_UNDECIDED:
                row["attr2"] = auto_attr
            elif auto_attr2 == manual_attr:
                row["attr2"] = ATTR_CODE_UNDECIDED
            # 手動補正は利用者が確定した値として扱い、自動信頼度と区別する。
            row["attr_conf"] = 1.0
    return result


def resolve_chart_path(root: Path, song: dict[str, Any]) -> Path | None:
    raw = str(song.get("path") or "").strip().strip('"')
    if not raw:
        return None
    raw = raw.replace("/", os.sep)
    candidates = [Path(raw)]
    if not Path(raw).is_absolute():
        candidates.extend([root / raw, root.parent / raw])
    for candidate in candidates:
        try:
            if candidate.is_file():
                return candidate.resolve()
        except OSError:
            continue
    return candidates[0].resolve() if candidates else None


def selected_table_charts(tables: list[TableInfo], table_ids: Iterable[str]) -> dict[str, ChartRef]:
    selected = set(table_ids)
    charts: dict[str, ChartRef] = {}
    for table in tables:
        if table.table_id in selected:
            merge_chart_maps(charts, table.all_charts())
    return charts


def _classification_from_feature_row(row: dict[str, Any]) -> dict[str, Any]:
    features = {name: row.get(name) for name in ATTR_NUMERIC_COLUMNS}
    features.update({name: row.get(name) for name in ATTR_INT_COLUMNS})
    attr, attr2, conf = classify_attr(features)
    return {
        "attr": attr,
        "attr2": attr2,
        "attr_conf": conf,
        "attr_sub": classify_subcategory(attr, features),
        "practice_low": classify_practice_priority(features),
    }


def reclassify_chart_analysis(
    analysis_db: Path,
    *,
    force: bool = False,
    progress: ItemProgressCallback | None = None,
    cancel_check: CancelCheck | None = None,
) -> dict[str, int]:
    """保存済み特徴量へ現在の解析ルールパックを適用する。

    BMSファイルは読み直さず、attr/attr2/信頼度/副分類/練習優先度だけを更新する。
    """
    if not analysis_db.exists():
        return {"target": 0, "reclassified": 0, "cached": 0, "skipped": 0}
    con = _analysis_connect(analysis_db)
    info = get_active_rule_pack_info()
    rule_hash = str(info.get("hash") or "")
    rows = con.execute(
        "SELECT * FROM chart_analysis WHERE analysis_version=? AND error=''",
        (ANALYSIS_VERSION,),
    ).fetchall()
    stats = {"target": len(rows), "reclassified": 0, "cached": 0, "skipped": 0}
    total = max(len(rows), 1)
    if progress:
        progress(0, total, "保存済み特徴量を再分類")
    for index, raw_row in enumerate(rows, start=1):
        if cancel_check and cancel_check():
            con.commit()
            con.close()
            raise AnalysisCancelled("差分解析を中止しました")
        row = dict(raw_row)
        if progress:
            progress(index - 1, total, Path(str(row.get("file_path") or "")).name or "再分類中")
        if not force and str(row.get("attr_rule_hash") or "") == rule_hash:
            stats["cached"] += 1
            continue
        if row.get("grid_bpm") is None or row.get("avg_chord") is None:
            stats["skipped"] += 1
            continue
        result = _classification_from_feature_row(row)
        con.execute(
            """
            UPDATE chart_analysis
               SET attr=?, attr2=?, attr_conf=?, attr_sub=?, practice_low=?,
                   attr_rule_pack_id=?, attr_rule_version=?, attr_rule_hash=?
             WHERE sha256=?
            """,
            (
                result["attr"], result["attr2"], result["attr_conf"],
                result["attr_sub"], result["practice_low"],
                str(info.get("pack_id") or ""), str(info.get("version") or ""), rule_hash,
                str(row.get("sha256") or ""),
            ),
        )
        stats["reclassified"] += 1
    con.commit()
    con.close()
    if progress:
        progress(total, total, "再分類完了")
    return stats


def update_chart_analysis(
    analysis_db: Path,
    root: Path,
    charts: dict[str, ChartRef],
    songs_by_sha: dict[str, dict[str, Any]],
    songs_by_md5: dict[str, dict[str, Any]],
    force: bool = False,
    progress: ItemProgressCallback | None = None,
    cancel_check: CancelCheck | None = None,
) -> dict[str, int]:
    reclass = reclassify_chart_analysis(
        analysis_db, force=False, cancel_check=cancel_check
    )
    con = _analysis_connect(analysis_db)
    existing = {str(row["sha256"]).lower(): dict(row) for row in con.execute("SELECT * FROM chart_analysis")}
    stats = {
        "target": 0, "analyzed": 0, "cached": 0, "missing": 0, "failed": 0,
        "reclassified": int(reclass.get("reclassified") or 0),
    }
    chart_values = list(charts.values())
    total = len(chart_values)
    if progress:
        progress(0, max(total, 1), "譜面解析を開始")
    for index, chart in enumerate(chart_values, start=1):
        if cancel_check and cancel_check():
            con.commit()
            con.close()
            raise AnalysisCancelled("差分解析を中止しました")
        if progress:
            progress(index - 1, max(total, 1), chart.title or chart.sha256 or chart.md5 or "譜面解析中")
        song = songs_by_sha.get(chart.sha256) if chart.sha256 else None
        if song is None and chart.md5:
            song = songs_by_md5.get(chart.md5)
        sha = str((song or {}).get("sha256") or chart.sha256 or "").lower()
        if not sha:
            continue
        stats["target"] += 1
        chart_path = resolve_chart_path(root, song or {})
        if chart_path is None or not chart_path.is_file():
            stats["missing"] += 1
            continue
        try:
            st = chart_path.stat()
        except OSError:
            stats["missing"] += 1
            continue
        old = existing.get(sha)
        if (
            not force and old
            and int(old.get("analysis_version") or 0) == ANALYSIS_VERSION
            and int(old.get("file_mtime_ns") or 0) == st.st_mtime_ns
            and int(old.get("file_size") or 0) == st.st_size
            and not str(old.get("error") or "")
        ):
            stats["cached"] += 1
            continue
        error = ""
        result: dict[str, Any] = {}
        try:
            result = analyze_bms_file(chart_path)
            result.update(analyze_attr_file(chart_path))
            stats["analyzed"] += 1
        except Exception as exc:
            error = str(exc)
            stats["failed"] += 1
        rule_info = get_active_rule_pack_info()
        result["attr_rule_pack_id"] = str(rule_info.get("pack_id") or "")
        result["attr_rule_version"] = str(rule_info.get("version") or "")
        result["attr_rule_hash"] = str(rule_info.get("hash") or "")
        attr_columns = [name for name, _t in _ATTR_COLUMN_DEFS + _ATTR_RULE_COLUMN_DEFS]
        column_sql = ", ".join(attr_columns)
        placeholder_sql = ",".join("?" for _ in attr_columns)
        update_sql = ", ".join(f"{name}=excluded.{name}" for name in attr_columns)
        con.execute(
            f"""
            INSERT INTO chart_analysis(
                sha256, file_path, file_mtime_ns, file_size, analysis_version,
                rhythm_family, dominant_division, effective_bpm, score16, score12,
                onset_count, analyzed_at, error, {column_sql}
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,{placeholder_sql})
            ON CONFLICT(sha256) DO UPDATE SET
                file_path=excluded.file_path,
                file_mtime_ns=excluded.file_mtime_ns,
                file_size=excluded.file_size,
                analysis_version=excluded.analysis_version,
                rhythm_family=excluded.rhythm_family,
                dominant_division=excluded.dominant_division,
                effective_bpm=excluded.effective_bpm,
                score16=excluded.score16,
                score12=excluded.score12,
                onset_count=excluded.onset_count,
                analyzed_at=excluded.analyzed_at,
                error=excluded.error,
                {update_sql}
            """,
            (
                sha, str(chart_path), st.st_mtime_ns, st.st_size, ANALYSIS_VERSION,
                result.get("rhythm_family"), result.get("dominant_division"), result.get("effective_bpm"),
                int(result.get("score16") or 0), int(result.get("score12") or 0),
                int(result.get("onset_count") or 0), int(time.time()), error,
            ) + tuple(result.get(name) for name, _t in _ATTR_COLUMN_DEFS + _ATTR_RULE_COLUMN_DEFS),
        )
        if index % 100 == 0:
            con.commit()
    con.commit()
    con.close()
    if progress:
        progress(max(total, 1), max(total, 1), "譜面解析完了")
    return stats


def _normalize_audio_name(value: str) -> str:
    return value.strip().strip('"').replace("\\", "/").casefold()


def _sound_channel(channel: str) -> bool:
    channel = channel.upper()
    if channel == "01":
        return True
    if len(channel) != 2:
        return False
    return channel[0] in "123456" and channel[1] in "123456789"


def analyze_bms_material_file(path: Path) -> dict[str, Any]:
    result: dict[str, Any] = {
        "wav_defs": {}, "used_ids": set(), "bpm_changes": 0, "stop_events": 0,
        "parse_error": "",
    }
    try:
        text = _read_bms_text(path)
        wav_defs: dict[str, str] = {}
        used: set[str] = set()
        bpm_changes = 0
        stop_events = 0
        for raw in text.splitlines():
            line = raw.strip()
            if not line.startswith("#"):
                continue
            m = re.match(r"^#WAV([0-9A-Z]{2})\s+(.+)$", line, flags=re.I)
            if m:
                wav_defs[m.group(1).upper()] = m.group(2).strip().strip('"')
                continue
            data_match = re.match(r"^#[0-9]{3}([0-9A-Z]{2}):([0-9A-Z]+)$", line, flags=re.I)
            if not data_match:
                continue
            channel = data_match.group(1).upper()
            payload = data_match.group(2).upper()
            tokens = [payload[i:i+2] for i in range(0, len(payload) - 1, 2)]
            nonzero = [token for token in tokens if token != "00"]
            if _sound_channel(channel):
                used.update(nonzero)
            if channel in {"03", "08"}:
                bpm_changes += len(nonzero)
            if channel == "09":
                stop_events += len(nonzero)
        result.update({"wav_defs": wav_defs, "used_ids": used, "bpm_changes": bpm_changes, "stop_events": stop_events})
    except Exception as exc:
        result["parse_error"] = str(exc)
    return result


def _material_connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute(
        """CREATE TABLE IF NOT EXISTS song_material_analysis (
            folder_path TEXT PRIMARY KEY,
            signature TEXT NOT NULL,
            result_json TEXT NOT NULL,
            analysis_version INTEGER NOT NULL,
            analyzed_at INTEGER NOT NULL,
            quick_signature TEXT NOT NULL DEFAULT ''
        )"""
    )
    columns = {str(row[1]) for row in con.execute("PRAGMA table_info(song_material_analysis)")}
    if "quick_signature" not in columns:
        con.execute("ALTER TABLE song_material_analysis ADD COLUMN quick_signature TEXT NOT NULL DEFAULT ''")
        con.commit()
    return con


def _chart_path_hint(root: Path, song: dict[str, Any]) -> Path | None:
    """Resolve a song path without repeatedly probing every file on disk.

    songdata.db normally stores an absolute chart path.  For relative paths we
    prefer the rian root, then its parent, matching resolve_chart_path's order.
    The hint is used only for grouping; actual parsing still verifies the file.
    """
    raw = str(song.get("path") or "").strip().strip('"')
    if not raw:
        return None
    raw = raw.replace("\\", os.sep).replace("/", os.sep)
    path = Path(raw)
    if path.is_absolute():
        return path
    primary = root / path
    secondary = root.parent / path
    try:
        if primary.exists():
            return primary
        if secondary.exists():
            return secondary
    except OSError:
        pass
    return primary


def song_folder_for_row(root: Path, song: dict[str, Any]) -> Path | None:
    chart_path = resolve_chart_path(root, song)
    return chart_path.parent if chart_path else None


def song_folder_hint(root: Path, song: dict[str, Any]) -> Path | None:
    chart_path = _chart_path_hint(root, song)
    return chart_path.parent if chart_path else None


def normalized_folder_key(path: Path | str) -> str:
    """Return a stable storage/display key while preserving path letter case.

    os.path.normcase() lowercases Windows paths.  Using that value as a public
    dictionary/SQLite key caused cache misses whenever callers supplied the
    original-cased path.  Storage keys therefore preserve case; comparisons use
    folder_compare_key() below.
    """
    try:
        return str(Path(path).resolve())
    except (OSError, RuntimeError):
        return os.path.abspath(str(path))


def folder_compare_key(path: Path | str) -> str:
    """Return a platform-aware, case-insensitive comparison key on Windows."""
    return os.path.normcase(os.path.abspath(str(path)))


def material_rows_by_folder(
    material_rows: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Index material rows by comparison key without changing public keys."""
    return {folder_compare_key(key): value for key, value in material_rows.items()}


def target_folder_keys_for_charts(
    root: Path,
    charts: dict[str, ChartRef],
    songs_by_sha: dict[str, dict[str, Any]],
    songs_by_md5: dict[str, dict[str, Any]],
) -> set[str]:
    result: set[str] = set()
    for chart in charts.values():
        song = songs_by_sha.get(chart.sha256) if chart.sha256 else None
        if song is None and chart.md5:
            song = songs_by_md5.get(chart.md5)
        if song is None:
            continue
        folder = song_folder_hint(root, song)
        if folder is not None:
            result.add(normalized_folder_key(folder))
    return result


def target_chart_keys(charts: dict[str, ChartRef]) -> set[str]:
    keys: set[str] = set()
    for chart in charts.values():
        normalized = chart.normalized()
        key = normalized.preferred_key()
        if key:
            keys.add(key)
    return keys


def material_quick_signature(folder: Path, chart_paths: Iterable[Path]) -> str:
    """Cheap cache key that avoids recursively stat-ing every audio file.

    It tracks the song folder, chart files and immediate child directories.
    Add/delete/rename operations update directory mtimes; direct chart edits are
    covered by chart stats.  The GUI's per-song reanalysis remains available for
    unusual in-place audio edits that do not touch a directory timestamp.
    """
    parts: list[str] = []
    try:
        stat = folder.stat()
        parts.append(f"D:{stat.st_mtime_ns}:{stat.st_size}")
    except OSError:
        parts.append("D:missing")
    for path in sorted(set(chart_paths), key=lambda x: str(x).casefold()):
        try:
            stat = path.stat()
            parts.append(f"C:{path.name.casefold()}:{stat.st_mtime_ns}:{stat.st_size}")
        except OSError:
            parts.append(f"C:{path.name.casefold()}:missing")
    try:
        with os.scandir(folder) as entries:
            for entry in entries:
                try:
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                    stat = entry.stat(follow_symlinks=False)
                    parts.append(f"S:{entry.name.casefold()}:{stat.st_mtime_ns}")
                except OSError:
                    continue
    except OSError:
        pass
    return hashlib.sha256("|".join(sorted(parts)).encode("utf-8", errors="replace")).hexdigest()


def _quick_state_not_newer_than(
    folder: Path,
    chart_paths: Iterable[Path],
    analyzed_at: int,
) -> bool:
    """Allow old v0.6.x cache rows to gain a quick signature without rescanning."""
    cutoff_ns = (int(analyzed_at) + 2) * 1_000_000_000
    paths = [folder, *chart_paths]
    try:
        with os.scandir(folder) as entries:
            paths.extend(Path(entry.path) for entry in entries if entry.is_dir(follow_symlinks=False))
    except OSError:
        pass
    for path in paths:
        try:
            if path.stat().st_mtime_ns > cutoff_ns:
                return False
        except OSError:
            return False
    return True


def _scan_audio_inventory(folder: Path) -> list[tuple[Path, int, int]]:
    """Enumerate audio files once, retaining stat data for signature/analysis.

    pathlib.rglob followed by Path.stat() caused the same directory tree to be
    walked twice for uncached songs. os.scandir provides file type and stat data
    in one traversal and is substantially faster on large Windows libraries.
    """
    inventory: list[tuple[Path, int, int]] = []
    stack = [folder]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                            continue
                        if not entry.is_file(follow_symlinks=False):
                            continue
                        path = Path(entry.path)
                        if path.suffix.casefold() not in AUDIO_EXTENSIONS:
                            continue
                        stat = entry.stat(follow_symlinks=False)
                        inventory.append((path, int(stat.st_mtime_ns), int(stat.st_size)))
                    except OSError:
                        continue
        except OSError:
            continue
    inventory.sort(key=lambda item: str(item[0]).casefold())
    return inventory


def material_folder_snapshot(
    folder: Path,
    chart_paths: Iterable[Path],
) -> tuple[list[tuple[Path, int, int]], str]:
    """Return one-pass audio inventory and a cache signature."""
    parts: list[str] = []
    try:
        stat = folder.stat()
        parts.append(f"D:{stat.st_mtime_ns}:{stat.st_size}")
    except OSError:
        parts.append("D:missing")
    for path in sorted(set(chart_paths), key=lambda x: str(x).casefold()):
        try:
            stat = path.stat()
            rel = path.relative_to(folder).as_posix().casefold()
            parts.append(f"C:{rel}:{stat.st_mtime_ns}:{stat.st_size}")
        except (OSError, ValueError):
            parts.append(f"C:{path.name.casefold()}:missing")
    inventory = _scan_audio_inventory(folder)
    for path, mtime_ns, size in inventory:
        try:
            rel = path.relative_to(folder).as_posix().casefold()
        except ValueError:
            rel = path.name.casefold()
        parts.append(f"A:{rel}:{mtime_ns}:{size}")
    signature = hashlib.sha256("|".join(parts).encode("utf-8", errors="replace")).hexdigest()
    return inventory, signature


def material_folder_signature(folder: Path, chart_paths: Iterable[Path]) -> str:
    """Compatibility wrapper used by one-song reanalysis."""
    _inventory, signature = material_folder_snapshot(folder, chart_paths)
    return signature


def analyze_song_material_folder(
    folder: Path,
    songs: list[dict[str, Any]],
    root: Path,
    chart_paths: list[Path] | None = None,
    audio_inventory: list[tuple[Path, int, int]] | None = None,
) -> dict[str, Any]:
    chart_paths = chart_paths if chart_paths is not None else [
        p for song in songs if (p := resolve_chart_path(root, song)) is not None
    ]
    audio_inventory = audio_inventory if audio_inventory is not None else _scan_audio_inventory(folder)
    audio_paths = [item[0] for item in audio_inventory]
    audio_sizes = {path: size for path, _mtime_ns, size in audio_inventory}
    actual_audio: dict[str, Path] = {}
    for path in audio_paths:
        try:
            relative_name = path.relative_to(folder).as_posix()
        except ValueError:
            relative_name = path.name
        actual_audio[_normalize_audio_name(relative_name)] = path

    def relative_audio_name(path: Path) -> str:
        try:
            return path.relative_to(folder).as_posix()
        except ValueError:
            return path.name

    referenced_names: set[str] = set()
    defined_names: set[str] = set()
    missing_names: set[str] = set()
    undefined_ids: set[str] = set()
    parse_errors: list[str] = []
    bpm_changes = 0
    stop_events = 0
    id_variants: dict[str, set[str]] = {}
    for path in chart_paths:
        row = analyze_bms_material_file(path)
        error = str(row.get("parse_error") or "")
        if error:
            parse_errors.append(f"{path.name}: {error}")
            continue
        defs: dict[str, str] = row.get("wav_defs") or {}
        used_ids: set[str] = row.get("used_ids") or set()
        bpm_changes += int(row.get("bpm_changes") or 0)
        stop_events += int(row.get("stop_events") or 0)
        for wav_id, name in defs.items():
            normalized = _normalize_audio_name(name)
            if not normalized:
                continue
            defined_names.add(normalized)
            id_variants.setdefault(wav_id, set()).add(normalized)
        for wav_id in used_ids:
            name = defs.get(wav_id)
            if not name:
                undefined_ids.add(wav_id)
                continue
            normalized = _normalize_audio_name(name)
            referenced_names.add(normalized)
            if normalized not in actual_audio:
                missing_names.add(normalized)
    zero_byte = sorted(relative_audio_name(path) for path in audio_paths if audio_sizes.get(path, -1) == 0)
    unused = sorted(relative_audio_name(path) for normalized, path in actual_audio.items() if normalized not in referenced_names)
    referenced_existing = sorted(relative_audio_name(actual_audio[name]) for name in referenced_names if name in actual_audio)
    chart_count = len(songs)
    key7_count = sum(1 for song in songs if int(song.get("mode") or 0) == 7)
    lengths = [int(song.get("length") or 0) for song in songs if int(song.get("length") or 0) > 0]
    material_ok = bool(key7_count and audio_paths and not missing_names and not undefined_ids and not zero_byte and not parse_errors)
    return {
        "folder_path": str(folder),
        "chart_count": chart_count,
        "key7_chart_count": key7_count,
        "audio_file_count": len(audio_paths),
        "defined_audio_count": len(defined_names),
        "referenced_audio_count": len(referenced_existing),
        "unused_candidate_count": len(unused),
        "unused_candidate_files": unused[:200],
        "missing_reference_count": len(missing_names) + len(undefined_ids),
        "missing_reference_files": sorted(missing_names)[:200],
        "undefined_wav_ids": sorted(undefined_ids)[:200],
        "zero_byte_audio_count": len(zero_byte),
        "zero_byte_audio_files": zero_byte[:200],
        "parse_error_count": len(parse_errors),
        "parse_errors": parse_errors[:100],
        "wav_definition_variant_ids": sum(1 for values in id_variants.values() if len(values) > 1),
        "bpm_change_events": bpm_changes,
        "stop_events": stop_events,
        "min_length_ms": min(lengths) if lengths else 0,
        "max_length_ms": max(lengths) if lengths else 0,
        "material_ok": material_ok,
    }



def update_song_material_analysis(
    analysis_db: Path,
    root: Path,
    songs_by_sha: dict[str, dict[str, Any]],
    songs_by_md5: dict[str, dict[str, Any]],
    force: bool = False,
    progress: ItemProgressCallback | None = None,
    target_folder_keys: set[str] | None = None,
) -> tuple[dict[str, dict[str, Any]], dict[str, int]]:
    """Analyze only folders that can appear in the selected generated tables.

    Storage/result keys preserve the original path casing.  A separate
    comparison key is used internally so Windows remains case-insensitive
    without leaking lower-cased paths into callers or SQLite rows.
    """
    grouped: dict[str, list[dict[str, Any]]] = {}
    folder_paths: dict[str, Path] = {}
    folder_compare_keys: dict[str, str] = {}
    target_compare_keys = (
        {folder_compare_key(key) for key in target_folder_keys}
        if target_folder_keys is not None
        else None
    )
    unique_rows = unique_song_rows(songs_by_sha, songs_by_md5)
    for song in unique_rows:
        if int(song.get("mode") or 0) != 7:
            continue
        folder = song_folder_hint(root, song)
        if folder is None:
            continue
        storage_key = normalized_folder_key(folder)
        compare_key = folder_compare_key(folder)
        if target_compare_keys is not None and compare_key not in target_compare_keys:
            continue
        grouped.setdefault(storage_key, []).append(song)
        folder_paths[storage_key] = folder
        folder_compare_keys[storage_key] = compare_key

    con = _material_connect(analysis_db)
    cached_source = con.execute(
        "SELECT folder_path, signature, result_json, analysis_version, "
        "quick_signature, analyzed_at FROM song_material_analysis"
    )
    cached_rows: dict[str, dict[str, Any]] = {}
    for row in cached_source:
        db_key = str(row[0])
        compare_key = folder_compare_key(db_key)
        if target_compare_keys is not None and compare_key not in target_compare_keys:
            continue
        cached_rows[compare_key] = {
            "db_key": db_key,
            "signature": str(row[1]),
            "result_json": str(row[2]),
            "analysis_version": int(row[3]),
            "quick_signature": str(row[4] or ""),
            "analyzed_at": int(row[5] or 0),
        }

    results: dict[str, dict[str, Any]] = {}
    stats = {
        "analyzed": 0,
        "cached": 0,
        "failed": 0,
        "quick_cached": 0,
        "legacy_cache_migrated": 0,
        "full_scans": 0,
        "folders": len(grouped),
        "library_folders_skipped": (
            max(0, len(target_compare_keys or set()) - len(grouped))
            if target_compare_keys is not None
            else 0
        ),
    }
    items = list(grouped.items())
    total = len(items)
    if progress:
        progress(0, max(total, 1), f"対象となる制作素材 {total:,}フォルダを確認")

    for index, (storage_key, songs) in enumerate(items, start=1):
        folder = folder_paths[storage_key]
        compare_key = folder_compare_keys[storage_key]
        if progress:
            progress(index - 1, max(total, 1), folder.name or str(folder))
        chart_paths = [
            path
            for song in songs
            if (path := resolve_chart_path(root, song)) is not None and path.is_file()
        ]
        quick_signature = material_quick_signature(folder, chart_paths)
        cached = cached_rows.get(compare_key)
        cached_result: dict[str, Any] | None = None
        if cached and not force and cached["analysis_version"] == MATERIAL_ANALYSIS_VERSION:
            try:
                cached_result = json.loads(cached["result_json"])
            except Exception:
                cached_result = None
            quick_matches = (
                bool(cached["quick_signature"])
                and cached["quick_signature"] == quick_signature
            )
            legacy_cache_safe = (
                not cached["quick_signature"]
                and cached_result is not None
                and _quick_state_not_newer_than(
                    folder, chart_paths, int(cached["analyzed_at"])
                )
            )
            if cached_result is not None and (quick_matches or legacy_cache_safe):
                results[storage_key] = cached_result
                stats["cached"] += 1
                stats["quick_cached"] += 1
                if legacy_cache_safe:
                    # Canonicalize old lower-cased Windows keys while adding the
                    # quick signature, avoiding duplicate rows differing only by case.
                    con.execute(
                        "DELETE FROM song_material_analysis WHERE folder_path=?",
                        (cached["db_key"],),
                    )
                    con.execute(
                        "INSERT OR REPLACE INTO song_material_analysis("
                        "folder_path,signature,result_json,analysis_version,analyzed_at,quick_signature"
                        ") VALUES (?,?,?,?,?,?)",
                        (
                            storage_key,
                            cached["signature"],
                            cached["result_json"],
                            cached["analysis_version"],
                            cached["analyzed_at"],
                            quick_signature,
                        ),
                    )
                    stats["legacy_cache_migrated"] += 1
                continue

        audio_inventory, signature = material_folder_snapshot(folder, chart_paths)
        stats["full_scans"] += 1
        if (
            not force
            and cached
            and cached_result is not None
            and cached["signature"] == signature
            and cached["analysis_version"] == MATERIAL_ANALYSIS_VERSION
        ):
            results[storage_key] = cached_result
            if cached["db_key"] != storage_key:
                con.execute(
                    "DELETE FROM song_material_analysis WHERE folder_path=?",
                    (cached["db_key"],),
                )
            con.execute(
                "INSERT OR REPLACE INTO song_material_analysis("
                "folder_path,signature,result_json,analysis_version,analyzed_at,quick_signature"
                ") VALUES (?,?,?,?,?,?)",
                (
                    storage_key,
                    cached["signature"],
                    cached["result_json"],
                    cached["analysis_version"],
                    cached["analyzed_at"],
                    quick_signature,
                ),
            )
            stats["cached"] += 1
            continue

        try:
            row = analyze_song_material_folder(
                folder,
                songs,
                root,
                chart_paths=chart_paths,
                audio_inventory=audio_inventory,
            )
            results[storage_key] = row
            if cached and cached["db_key"] != storage_key:
                con.execute(
                    "DELETE FROM song_material_analysis WHERE folder_path=?",
                    (cached["db_key"],),
                )
            con.execute(
                "INSERT OR REPLACE INTO song_material_analysis("
                "folder_path,signature,result_json,analysis_version,analyzed_at,quick_signature"
                ") VALUES (?,?,?,?,?,?)",
                (
                    storage_key,
                    signature,
                    json.dumps(row, ensure_ascii=False),
                    MATERIAL_ANALYSIS_VERSION,
                    int(time.time()),
                    quick_signature,
                ),
            )
            stats["analyzed"] += 1
        except Exception as exc:
            results[storage_key] = {
                "folder_path": str(folder),
                "material_ok": False,
                "parse_error_count": 1,
                "parse_errors": [str(exc)],
            }
            stats["failed"] += 1

    con.commit()
    con.close()
    if progress:
        progress(max(total, 1), max(total, 1), "対象フォルダの制作素材解析完了")
    return results, stats

def read_song_material_analysis(analysis_db: Path | None) -> dict[str, dict[str, Any]]:
    if analysis_db is None or not analysis_db.exists():
        return {}
    try:
        con = _material_connect(analysis_db)
        rows = con.execute("SELECT folder_path,result_json FROM song_material_analysis WHERE analysis_version=?", (MATERIAL_ANALYSIS_VERSION,)).fetchall()
        con.close()
        result = {}
        for key, raw in rows:
            try:
                result[str(key)] = json.loads(str(raw))
            except Exception:
                continue
        return result
    except sqlite3.Error:
        return {}


def chart_difficulty_axis_lookup(
    tables: Iterable[TableInfo],
    registry: dict[str, Any],
) -> dict[str, float]:
    """Resolve each chart to one representative common-axis position.

    Multiple table memberships are combined by the median of mapping centres.
    Mappings explicitly excluded from gap detection are ignored.
    """
    candidates: dict[str, list[float]] = {}
    for table in tables:
        if table.error or table.table_id in {INSTANT_TABLE_ID, MAKER_TABLE_ID}:
            continue
        for level_name, level in table.levels.items():
            mapping = resolve_table_level_range(table, level_name, registry)
            if not mapping or mapping.get("usable_for_gap_detection") is False:
                continue
            try:
                centre = (float(mapping["axis_min"]) + float(mapping["axis_max"])) / 2.0
            except (KeyError, TypeError, ValueError):
                continue
            for key in level.charts:
                candidates.setdefault(key, []).append(centre)
    return {key: float(median(values)) for key, values in candidates.items() if values}


def _maker_representative(songs: list[dict[str, Any]]) -> dict[str, Any]:
    return min(
        songs,
        key=lambda song: (
            int(song.get("level") or 9999),
            int(song.get("notes") or 9999999),
            str(song.get("path") or song.get("file") or "").casefold(),
        ),
    )


def build_maker_table(
    root: Path,
    songs_by_sha: dict[str, dict[str, Any]],
    songs_by_md5: dict[str, dict[str, Any]],
    material_rows: dict[str, dict[str, Any]],
    tables: Iterable[TableInfo] | None = None,
    registry: dict[str, Any] | None = None,
    scores: dict[str, dict[str, Any]] | None = None,
    progress: ItemProgressCallback | None = None,
) -> TableInfo:
    level_names = [
        "制作素材・良好",
        "制作素材・未使用候補音源あり",
        "制作素材・要確認",
        "譜面数・標準譜面のみ（差分なし候補）",
        "譜面数・7KEY 1譜面のみ（差分なし候補）",
        "譜面数・7KEY 1～2譜面",
        "譜面数・7KEY 3～4譜面",
        "譜面数・7KEY 5～6譜面",
        "プレイ密度・1譜面あたり0回",
        "プレイ密度・1譜面あたり1回未満",
        "プレイ密度・1譜面あたり2回未満",
        "差分候補・同曲のINSANE譜面が1つのみ",
        "差分候補・表所属の上位譜面なし",
        "差分候補・表所属の下位譜面なし",
        "差分候補・上3段階以内に譜面なし",
        "差分候補・下3段階以内に譜面なし",
        "差分候補・難度空白3段階以上",
        "差分候補・難度空白5段階以上",
        "差分候補・難度空白7段階以上",
        "参考・表外の高難度譜面あり",
    ]
    levels = {name: TableLevel(name=name) for name in level_names}
    songs_by_folder: dict[str, list[dict[str, Any]]] = {}
    folder_display_paths: dict[str, str] = {}
    for song in unique_song_rows(songs_by_sha, songs_by_md5):
        if int(song.get("mode") or 0) != 7:
            continue
        folder = song_folder_for_row(root, song)
        if folder is None:
            continue
        folder_key = folder_compare_key(folder)
        folder_display_paths.setdefault(folder_key, normalized_folder_key(folder))
        songs_by_folder.setdefault(folder_key, []).append(song)

    material_lookup = material_rows_by_folder(material_rows)
    registry = registry or load_difficulty_scale_registry()
    axis_by_key = chart_difficulty_axis_lookup(tables or [], registry)
    scores = scores or {}

    def add(level_name: str, song: dict[str, Any]) -> None:
        chart = chart_ref_from_song(song).normalized()
        key = chart.preferred_key()
        if key:
            levels[level_name].charts[key] = chart

    folder_items = list(songs_by_folder.items())
    total_folders = len(folder_items)
    if progress:
        progress(0, max(total_folders, 1), "制作候補の条件判定を開始")
    for folder_index, (folder_key, songs) in enumerate(folder_items, start=1):
        folder_display = folder_display_paths.get(folder_key, folder_key)
        if progress:
            progress(folder_index - 1, max(total_folders, 1), Path(folder_display).name or folder_display)
        songs = sorted(songs, key=lambda row: str(row.get("path") or "").casefold())
        representative = _maker_representative(songs)
        material = material_lookup.get(folder_key)
        if material:
            if bool(material.get("material_ok")):
                add("制作素材・良好", representative)
                if int(material.get("unused_candidate_count") or 0) > 0:
                    add("制作素材・未使用候補音源あり", representative)
            else:
                add("制作素材・要確認", representative)

        chart_count = len(songs)
        standard_only_candidate = (
            1 <= chart_count <= 4
            and all(int(song.get("difficulty") or 0) <= 4 for song in songs)
            and all(int(song.get("level") or 0) <= 12 for song in songs)
        )
        if standard_only_candidate:
            add("譜面数・標準譜面のみ（差分なし候補）", representative)
        if chart_count == 1:
            add("譜面数・7KEY 1譜面のみ（差分なし候補）", representative)
        if 1 <= chart_count <= 2:
            add("譜面数・7KEY 1～2譜面", representative)
        elif 3 <= chart_count <= 4:
            add("譜面数・7KEY 3～4譜面", representative)
        elif 5 <= chart_count <= 6:
            add("譜面数・7KEY 5～6譜面", representative)

        total_plays = 0
        for song in songs:
            sha = str(song.get("sha256") or "").lower()
            total_plays += int((scores.get(sha) or {}).get("playcount") or 0) if sha else 0
        plays_per_chart = total_plays / chart_count if chart_count else 0.0
        if total_plays == 0:
            add("プレイ密度・1譜面あたり0回", representative)
        if plays_per_chart < 1.0:
            add("プレイ密度・1譜面あたり1回未満", representative)
        if plays_per_chart < 2.0:
            add("プレイ密度・1譜面あたり2回未満", representative)

        known: list[tuple[float, dict[str, Any]]] = []
        table_external_advanced: list[dict[str, Any]] = []
        insane_songs: list[dict[str, Any]] = []
        for song in songs:
            chart = chart_ref_from_song(song).normalized()
            key = chart.preferred_key()
            is_insane = int(song.get("difficulty") or 0) == INSANE_DIFFICULTY
            if is_insane:
                insane_songs.append(song)
            if key and key in axis_by_key:
                known.append((axis_by_key[key], song))
            elif is_insane:
                table_external_advanced.append(song)
        if len(insane_songs) == 1:
            add("差分候補・同曲のINSANE譜面が1つのみ", insane_songs[0])
        if table_external_advanced:
            add("参考・表外の高難度譜面あり", representative)
        if not known:
            continue
        known.sort(key=lambda item: (item[0], int(item[1].get("notes") or 0), str(item[1].get("path") or "")))
        add("差分候補・表所属の下位譜面なし", known[0][1])
        add("差分候補・表所属の上位譜面なし", known[-1][1])

        # Work with one representative chart at each axis position so duplicate
        # table memberships and same-level charts do not create zero gaps.
        by_axis: dict[float, dict[str, Any]] = {}
        for axis, song in known:
            by_axis.setdefault(axis, song)
        points = sorted(by_axis.items())
        for index, (axis, song) in enumerate(points):
            if index + 1 >= len(points):
                add("差分候補・上3段階以内に譜面なし", song)
            else:
                upper_axis, _upper_song = points[index + 1]
                if upper_axis - axis > 3.0:
                    add("差分候補・上3段階以内に譜面なし", song)
            if index == 0:
                add("差分候補・下3段階以内に譜面なし", song)
            else:
                lower_axis, _lower_song = points[index - 1]
                if axis - lower_axis > 3.0:
                    add("差分候補・下3段階以内に譜面なし", song)
        for (lower_axis, lower_song), (upper_axis, _upper_song) in zip(points, points[1:]):
            gap = upper_axis - lower_axis
            for threshold in (3, 5, 7):
                if gap >= threshold:
                    add(f"差分候補・難度空白{threshold}段階以上", lower_song)

    if progress:
        progress(max(total_folders, 1), max(total_folders, 1), "制作候補の条件判定完了")
    return TableInfo(
        table_id=MAKER_TABLE_ID,
        url="bmscf://maker",
        name="差分制作者向けフィルタ",
        tag="差分制作",
        source="virtual",
        levels=levels,
    )


MAKER_METRIC_COLUMNS: dict[str, str] = {
    "maker_axis_known": "axis_known",
    "maker_material_ok": "material_ok",
    "maker_needs_review": "needs_review",
    "maker_unused_audio_count": "unused_audio_count",
    "maker_chart_count": "chart_count",
    "maker_other_advanced_count": "other_advanced_count",
    "maker_has_higher": "has_higher",
    "maker_has_lower": "has_lower",
    "maker_upper_gap": "upper_gap",
    "maker_lower_gap": "lower_gap",
    "maker_unknown_count": "unknown_count",
}


def build_maker_chart_metrics(
    root: Path,
    songs_by_sha: dict[str, dict[str, Any]],
    songs_by_md5: dict[str, dict[str, Any]],
    material_rows: dict[str, dict[str, Any]],
    tables: Iterable[TableInfo],
    registry: dict[str, Any] | None = None,
    progress: ItemProgressCallback | None = None,
    target_folder_keys: set[str] | None = None,
    target_keys: set[str] | None = None,
) -> tuple[dict[str, dict[str, Any]], list[tuple[Any, ...]], dict[str, int]]:
    """Build per-chart maker metrics only for generated-table candidates.

    All 7KEY charts in a target song folder are still considered when looking
    for higher/lower charts, but metrics are published only for charts that can
    actually appear in the selected base/cross tables.
    """
    songs_by_folder: dict[str, list[dict[str, Any]]] = {}
    folder_display_paths: dict[str, str] = {}
    target_compare_keys = (
        {folder_compare_key(key) for key in target_folder_keys}
        if target_folder_keys is not None
        else None
    )
    for song in unique_song_rows(songs_by_sha, songs_by_md5):
        if int(song.get("mode") or 0) != 7:
            continue
        folder = song_folder_hint(root, song)
        if folder is None:
            continue
        folder_key = folder_compare_key(folder)
        if target_compare_keys is not None and folder_key not in target_compare_keys:
            continue
        folder_display_paths.setdefault(folder_key, normalized_folder_key(folder))
        songs_by_folder.setdefault(folder_key, []).append(song)

    material_lookup = material_rows_by_folder(material_rows)
    registry = registry or load_difficulty_scale_registry()
    axis_by_key = chart_difficulty_axis_lookup(tables, registry)
    lookup: dict[str, dict[str, Any]] = {}
    rows: list[tuple[Any, ...]] = []
    folders = list(songs_by_folder.items())
    if progress:
        progress(0, max(len(folders), 1), "対象楽曲の差分構成を判定")

    for folder_index, (folder_key, songs) in enumerate(folders, start=1):
        folder_display = folder_display_paths.get(folder_key, folder_key)
        if progress:
            progress(folder_index - 1, max(len(folders), 1), Path(folder_display).name or folder_display)
        unique: dict[str, dict[str, Any]] = {}
        for song in songs:
            key = chart_ref_from_song(song).normalized().preferred_key()
            if key:
                unique.setdefault(key, song)
        chart_count = len(unique)
        known_axes = {key: axis_by_key[key] for key in unique if key in axis_by_key}
        insane_keys = {
            key for key, song in unique.items()
            if int(song.get("difficulty") or 0) == INSANE_DIFFICULTY
        }
        high_difficulty_keys = {
            key for key, song in unique.items()
            if int(song.get("difficulty") or 0) >= HIGH_DIFFICULTY_MIN
        }
        # 「表外の高難度譜面」は、INSANE種別だが登録済み表から共通軸へ
        # 位置付けられなかった譜面だけを数える。N/H/A等は含めない。
        unknown_count = len(high_difficulty_keys - set(known_axes))
        material_present = folder_key in material_lookup
        material = material_lookup.get(folder_key) or {}
        material_ok = 1 if material_present and bool(material.get("material_ok")) else 0
        needs_review = 1 if material_present and not material_ok else 0
        unused_audio_count = int(material.get("unused_candidate_count") or 0) if material_present else 0

        for key, song in unique.items():
            if target_keys is not None and key not in target_keys:
                continue
            axis = known_axes.get(key)
            higher = sorted(
                value - axis
                for other_key, value in known_axes.items()
                if other_key != key and axis is not None and value > axis
            )
            lower = sorted(
                axis - value
                for other_key, value in known_axes.items()
                if other_key != key and axis is not None and value < axis
            )
            has_higher = 1 if higher else 0
            has_lower = 1 if lower else 0
            upper_gap = float(higher[0]) if higher else -1.0
            lower_gap = float(lower[0]) if lower else -1.0
            has_near_higher = 1 if higher and higher[0] <= 3.0 else 0
            has_near_lower = 1 if lower and lower[0] <= 3.0 else 0
            # 現在譜面自身がINSANEで、同曲に他のINSANEが無い場合だけ0。
            # 標準譜面が表へ登録されている場合の誤検出を避ける。
            other_advanced_count = (
                len(insane_keys - {key}) if key in insane_keys else len(insane_keys) + 1
            )
            axis_known = 1 if axis is not None else 0
            metric = {
                "maker_axis_known": bool(axis_known),
                "maker_material_ok": bool(material_ok),
                "maker_needs_review": bool(needs_review),
                "maker_unused_audio_count": unused_audio_count,
                "maker_chart_count": chart_count,
                "maker_other_advanced_count": other_advanced_count,
                "maker_has_higher": bool(has_higher),
                "maker_has_lower": bool(has_lower),
                "maker_upper_gap": upper_gap,
                "maker_lower_gap": lower_gap,
                "maker_has_near_higher": bool(has_near_higher),
                "maker_has_near_lower": bool(has_near_lower),
                "maker_unknown_count": unknown_count,
                "maker_metric_version": MAKER_METRIC_VERSION,
            }
            chart = chart_ref_from_song(song).normalized()
            if chart.sha256:
                lookup[chart.sha256] = metric
            if chart.md5:
                lookup[f"md5:{chart.md5}"] = metric
            for ref in chart.hash_refs():
                rows.append((
                    ref.hash_type,
                    ref.value.lower(),
                    axis_known,
                    material_ok,
                    needs_review,
                    unused_audio_count,
                    chart_count,
                    other_advanced_count,
                    has_higher,
                    has_lower,
                    upper_gap,
                    lower_gap,
                    unknown_count,
                    MAKER_METRIC_VERSION,
                ))

    if progress:
        progress(max(len(folders), 1), max(len(folders), 1), "対象楽曲の差分構成判定完了")
    stats = {
        "folders": len(folders),
        "charts": sum(1 for key in lookup if not key.startswith("md5:")),
        "rows": len(rows),
    }
    return lookup, rows, stats


def reanalyze_material_for_chart(
    song_db: Path,
    analysis_db: Path,
    root: Path,
    sha256: str = "",
    md5: str = "",
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    songs_by_sha, songs_by_md5 = read_song_rows(song_db)
    song = songs_by_sha.get((sha256 or "").lower()) if sha256 else None
    if song is None and md5:
        song = songs_by_md5.get(md5.lower())
    if song is None:
        return None, None
    folder = song_folder_for_row(root, song)
    if folder is None:
        return song, None
    folder_key = normalized_folder_key(folder)
    folder_match_key = folder_compare_key(folder)
    same_folder: list[dict[str, Any]] = []
    for row in unique_song_rows(songs_by_sha, songs_by_md5):
        row_folder = song_folder_hint(root, row)
        if row_folder is None:
            continue
        if folder_compare_key(row_folder) == folder_match_key:
            same_folder.append(row)
    chart_paths = [
        p for row in same_folder
        if (p := resolve_chart_path(root, row)) is not None and p.is_file()
    ]
    audio_inventory, signature = material_folder_snapshot(folder, chart_paths)
    material = analyze_song_material_folder(
        folder, same_folder, root, chart_paths=chart_paths, audio_inventory=audio_inventory
    )
    con = _material_connect(analysis_db)
    quick_signature = material_quick_signature(folder, chart_paths)
    con.execute(
        "INSERT OR REPLACE INTO song_material_analysis("
        "folder_path,signature,result_json,analysis_version,analyzed_at,quick_signature"
        ") VALUES (?,?,?,?,?,?)",
        (
            folder_key, signature, json.dumps(material, ensure_ascii=False),
            MATERIAL_ANALYSIS_VERSION, int(time.time()), quick_signature,
        ),
    )
    con.commit()
    con.close()
    return song, material


def find_material_for_chart(
    song_db: Path,
    analysis_db: Path,
    root: Path,
    sha256: str = "",
    md5: str = "",
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    if not song_db.exists():
        return None, None
    columns = "lower(sha256) AS sha256, lower(md5) AS md5, title, artist, level, notes, minbpm, maxbpm, difficulty, mode, favorite, length, path"
    song: dict[str, Any] | None = None
    try:
        con = sqlite3.connect(f"file:{song_db.as_posix()}?mode=ro", uri=True, timeout=3)
        con.row_factory = sqlite3.Row
        if sha256:
            row = con.execute(f"SELECT {columns} FROM song WHERE lower(sha256)=? LIMIT 1", ((sha256 or "").lower(),)).fetchone()
        elif md5:
            row = con.execute(f"SELECT {columns} FROM song WHERE lower(md5)=? LIMIT 1", ((md5 or "").lower(),)).fetchone()
        else:
            row = None
        con.close()
        if row is not None:
            song = dict(row)
    except sqlite3.Error:
        return None, None
    if song is None:
        return None, None
    folder = song_folder_for_row(root, song)
    if folder is None or not analysis_db.exists():
        return song, None
    key = folder_compare_key(folder)
    try:
        con = _material_connect(analysis_db)
        rows = con.execute(
            "SELECT folder_path,result_json FROM song_material_analysis WHERE analysis_version=?",
            (MATERIAL_ANALYSIS_VERSION,),
        ).fetchall()
        con.close()
        for stored_path, raw in rows:
            if folder_compare_key(str(stored_path)) == key:
                return song, json.loads(str(raw))
        return song, None
    except Exception:
        return song, None


def density_rank_groups(
    charts: dict[str, ChartRef],
    songs_by_sha: dict[str, dict[str, Any]],
    songs_by_md5: dict[str, dict[str, Any]],
) -> dict[str, set[str]]:
    ranked: list[tuple[float, str]] = []
    for key, chart in charts.items():
        song = songs_by_sha.get(chart.sha256) if chart.sha256 else None
        if song is None and chart.md5:
            song = songs_by_md5.get(chart.md5)
        if not song:
            continue
        notes = int(song.get("notes") or 0)
        length = int(song.get("length") or 0)
        if notes <= 0 or length <= 0:
            continue
        ranked.append((notes * 1000.0 / length, key))
    ranked.sort(key=lambda item: (item[0], item[1]))
    result: dict[str, set[str]] = {key: set() for _density, key in ranked}
    count = len(ranked)
    if count == 0:
        return result
    for percent in (10, 25, 50):
        size = max(1, ceil(count * percent / 100.0))
        for _density, key in ranked[-size:]:
            result[key].add(f"上位{percent}%")
        for _density, key in ranked[:size]:
            result[key].add(f"下位{percent}%")
    edge = max(0, ceil(count * 25 / 100.0))
    middle = ranked[edge:count - edge] if count - edge > edge else []
    for _density, key in middle:
        result[key].add("中央50%")
    return result


def canonicalize_table_charts(
    tables: list[TableInfo],
    songs_by_sha: dict[str, dict[str, Any]],
    songs_by_md5: dict[str, dict[str, Any]],
    progress: ItemProgressCallback | None = None,
) -> None:
    total = sum(len(table.levels) for table in tables)
    processed = 0
    if progress:
        progress(0, max(total, 1), "難易度表の所属を照合中")
    for table in tables:
        for level in table.levels.values():
            normalized: dict[str, ChartRef] = {}
            for chart in level.charts.values():
                chart = chart.normalized()
                song = None
                if chart.sha256:
                    song = songs_by_sha.get(chart.sha256)
                if song is None and chart.md5:
                    song = songs_by_md5.get(chart.md5)
                if song:
                    chart.sha256 = (song.get("sha256") or chart.sha256 or "").lower()
                    chart.md5 = (song.get("md5") or chart.md5 or "").lower()
                    if not chart.title:
                        chart.title = song.get("title") or ""
                    if not chart.artist:
                        chart.artist = song.get("artist") or ""
                key = chart.preferred_key()
                if key:
                    merge_chart_maps(normalized, {key: chart})
            level.charts = normalized
            processed += 1
            if progress:
                progress(processed, max(total, 1), f"{table.name} / {level.name}")
    if progress:
        progress(max(total, processed), max(total, processed, 1), "難易度表の所属照合完了")


def extract_level_number(level_name: str) -> str | None:
    text = (level_name or "").strip()
    match = re.search(r"(-?\d+(?:\.\d+)?)\s*$", text)
    return match.group(1) if match else None


def same_number_level_charts(
    table: TableInfo,
    base_level_name: str,
    base_table: TableInfo | None = None,
    registry: dict[str, Any] | None = None,
) -> dict[str, ChartRef]:
    number = extract_level_number(base_level_name)
    if number is None:
        return {}
    base_family = (
        difficulty_same_number_family(base_table, base_level_name, registry)
        if base_table is not None else None
    )
    result: dict[str, ChartRef] = {}
    for level_name, level in table.levels.items():
        if extract_level_number(level_name) != number:
            continue
        if base_family is not None:
            source_family = difficulty_same_number_family(table, level_name, registry)
            # A known sl/st destination only accepts a positively identified
            # member of the same series.  Unknown sources are not duplicated
            # into both slN and stN.
            if source_family != base_family:
                continue
        merge_chart_maps(result, level.charts)
    return result


def compare_values(actual: Any, op: str, expected: Any) -> bool:
    if op == "=":
        return actual == expected
    if op == "!=":
        return actual != expected
    if op == ">=":
        return actual >= expected
    if op == "<=":
        return actual <= expected
    if op == ">":
        return actual > expected
    if op == "<":
        return actual < expected
    raise ToolError(f"未対応の比較演算子です: {op}")


def parse_bool(value: str) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on", "はい", "済"}


CHART_ANALYSIS_FILTER_FIELDS: frozenset[str] = frozenset({
    "rhythm_family", "effective_bpm",
    "attr", "attr_any", "attr_conf", "grid_bpm", "avg_chord",
    "micro_rate", "subgrid_rate", "stream_sec", "d10", "d20",
    "last_kill", "recovery_sec",
})


def field_requires_chart_analysis(field_name: str) -> bool:
    return field_name in CHART_ANALYSIS_FILTER_FIELDS


def presets_require_chart_analysis(presets: Iterable[Preset]) -> bool:
    return any(
        condition.enabled and field_requires_chart_analysis(condition.field)
        for preset in presets
        for condition in preset.conditions
    )


def evaluate_condition(
    condition: Condition,
    song: dict[str, Any] | None,
    score: dict[str, Any] | None,
    analysis: dict[str, Any] | None = None,
    density_groups: set[str] | None = None,
    now: int | None = None,
) -> bool:
    if not condition.enabled:
        return True
    field_name = condition.field
    op = condition.op
    value = condition.value
    now = now or int(time.time())
    played = bool(score and int(score.get("playcount") or 0) > 0)
    if field_name == "played":
        return compare_values(played, op, parse_bool(value))
    if field_name == "clear":
        actual = int(score.get("clear") or 0) if score else 0
        expected = CLEAR_TYPES.get(str(value).strip().upper())
        if expected is None:
            expected = int(float(value))
        return compare_values(actual, op, expected)
    if field_name == "best_rank":
        actual_name = score_rate_to_rank(float((score or {}).get("score_rate") or 0.0), played=played)
        expected_name = str(value).strip().upper()
        if expected_name not in RANK_VALUES:
            raise ToolError(f"ランクが不正です: {value}")
        return compare_values(RANK_VALUES[actual_name], op, RANK_VALUES[expected_name])
    if field_name == "minbp":
        actual = score.get("minbp") if score else None
        if actual is None or int(actual) >= 2_000_000_000:
            return False
        return compare_values(int(actual), op, int(float(value)))
    if field_name == "miss_rate":
        actual_bp = score.get("minbp") if score else None
        notes = int((song or {}).get("notes") or 0)
        if actual_bp is None or int(actual_bp) >= 2_000_000_000 or notes <= 0:
            return False
        actual = int(actual_bp) * 100.0 / notes
        return compare_values(actual, op, float(value))
    if field_name == "playcount":
        actual = int(score.get("playcount") or 0) if score else 0
        return compare_values(actual, op, int(float(value)))
    if field_name == "last_play_days":
        if not played:
            return False
        date = int(score.get("date") or 0)
        days = int(max(0, now - date) // 86400) if date > 0 else 999999
        return compare_values(days, op, int(float(value)))
    if field_name == "score_rate":
        actual = float(score.get("score_rate") or 0.0) if score else 0.0
        return compare_values(actual, op, float(value))
    if field_name == "density_rank":
        actual = str(value) in (density_groups or set())
        return actual if op == "=" else not actual
    if field_name == "rhythm_family":
        if not analysis or analysis.get("rhythm_family") not in {12, 16} or analysis.get("error"):
            return False
        actual = "16分系" if int(analysis["rhythm_family"]) == 16 else "12分系"
        return compare_values(actual, op, str(value))
    if field_name == "effective_bpm":
        if not analysis or analysis.get("effective_bpm") is None or analysis.get("error"):
            return False
        return compare_values(float(analysis["effective_bpm"]), op, float(value))
    if field_name == "practice_low":
        if not analysis or analysis.get("error"):
            return False
        return compare_values(bool(int(analysis.get("practice_low") or 0)), op, parse_bool(value))
    if field_name == "attr_sub":
        if not analysis or analysis.get("attr_sub") is None or analysis.get("error"):
            return False
        label = str(value).strip()
        expected_sub = SUBCAT_NONE if label in {"副分類なし", ""} else SUBCAT_CODES.get(label, SUBCAT_NONE)
        return compare_values(int(analysis.get("attr_sub") or 0), op, expected_sub)
    if field_name in {"attr", "attr_any"}:
        if not analysis or analysis.get("attr") is None or analysis.get("error"):
            return False
        label = str(value).strip()
        if label == "中速厚め":
            label = "16分乱打"
        expected = ATTR_CODES.get(label, ATTR_CODE_UNDECIDED)
        primary = int(analysis.get("attr") or 0)
        if field_name == "attr":
            return compare_values(primary, op, expected)
        secondary = int(analysis.get("attr2") or 0)
        matched = primary == expected or (secondary != ATTR_CODE_UNDECIDED and secondary == expected)
        return matched if op == "=" else not matched
    if field_name in {"attr_conf", "grid_bpm", "avg_chord", "micro_rate", "subgrid_rate",
                      "stream_sec", "d10", "d20", "last_kill", "recovery_sec"}:
        if not analysis or analysis.get(field_name) is None or analysis.get("error"):
            return False
        return compare_values(float(analysis[field_name]), op, float(value))
    if field_name in MAKER_METRIC_COLUMNS or field_name in {"maker_has_near_higher", "maker_has_near_lower"}:
        if not analysis or field_name not in analysis:
            return False
        actual = analysis.get(field_name)
        if FIELD_DEFS[field_name]["type"] == "bool":
            return compare_values(bool(actual), op, parse_bool(value))
        return compare_values(float(actual or 0), op, float(value))
    if field_name in {"song_level", "notes", "minbpm", "maxbpm"}:
        if song is None:
            return False
        column = "level" if field_name == "song_level" else field_name
        actual = int(song.get(column) or 0)
        return compare_values(actual, op, int(float(value)))
    if field_name in {"title", "artist"}:
        actual = str((song or {}).get(field_name) or "")
        expected = str(value or "")
        if op == "含む":
            return expected.casefold() in actual.casefold()
        if op == "含まない":
            return expected.casefold() not in actual.casefold()
        return compare_values(actual.casefold(), op, expected.casefold())
    raise ToolError(f"未対応のフィルター項目です: {field_name}")


def evaluate_preset(
    preset: Preset,
    song: dict[str, Any] | None,
    score: dict[str, Any] | None,
    analysis: dict[str, Any] | None = None,
    density_groups: set[str] | None = None,
    now: int | None = None,
) -> bool:
    enabled = [c for c in preset.conditions if c.enabled]
    if not enabled:
        return True
    results = [
        evaluate_condition(c, song, score, analysis=analysis, density_groups=density_groups, now=now)
        for c in enabled
    ]
    return any(results) if preset.join.upper() == "OR" else all(results)


def chart_local_data(
    chart: ChartRef,
    songs_by_sha: dict[str, dict[str, Any]],
    songs_by_md5: dict[str, dict[str, Any]],
    scores: dict[str, dict[str, Any]],
    analyses: dict[str, dict[str, Any]] | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, dict[str, Any] | None]:
    song = songs_by_sha.get(chart.sha256) if chart.sha256 else None
    if song is None and chart.md5:
        song = songs_by_md5.get(chart.md5)
    score = None
    analysis = None
    sha = ""
    if song:
        sha = song.get("sha256") or ""
    if not sha:
        sha = chart.sha256
    if sha:
        normalized = sha.lower()
        score = scores.get(normalized)
        analysis = (analyses or {}).get(normalized)
    if analysis is None:
        md5 = str((song or {}).get("md5") or chart.md5 or "").lower()
        if md5:
            analysis = (analyses or {}).get(f"md5:{md5}")
    return song, score, analysis


def filter_charts(
    charts: dict[str, ChartRef],
    preset: Preset | None,
    songs_by_sha: dict[str, dict[str, Any]],
    songs_by_md5: dict[str, dict[str, Any]],
    scores: dict[str, dict[str, Any]],
    analyses: dict[str, dict[str, Any]] | None = None,
    density_groups_by_key: dict[str, set[str]] | None = None,
) -> dict[str, ChartRef]:
    if preset is None:
        return dict(charts)
    result: dict[str, ChartRef] = {}
    now = int(time.time())
    for key, chart in charts.items():
        song, score, analysis = chart_local_data(chart, songs_by_sha, songs_by_md5, scores, analyses)
        if evaluate_preset(
            preset,
            song,
            score,
            analysis=analysis,
            density_groups=(density_groups_by_key or {}).get(key, set()),
            now=now,
        ):
            result[key] = chart
    return result


def stable_view_id(
    base_table_id: str,
    level_name: str,
    combo_key: str,
    cross_table_id: str,
    preset_id: str,
) -> str:
    raw = "|".join([base_table_id, level_name, combo_key, cross_table_id, preset_id])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def stable_base_view_id(
    base_table_id: str,
    level_name: str,
    combo_key: str,
    cross_table_id: str,
) -> str:
    raw = "|".join([base_table_id, level_name, combo_key, cross_table_id])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _clone_table_levels(table: TableInfo) -> dict[str, TableLevel]:
    return {
        name: TableLevel(name, dict(level.charts))
        for name, level in table.levels.items()
    }


def _qualified_level_names(table: TableInfo, level_names: Iterable[str]) -> list[str]:
    """Return readable level labels, adding the table tag only to bare numeric levels."""
    result: list[str] = []
    tag = str(table.tag or "").strip()
    for raw_name in level_names:
        name = str(raw_name or "").strip()
        if not name:
            continue
        if (
            tag
            and table.source != "generated"
            and re.fullmatch(r"-?\d+(?:\.\d+)?[+?-]?", name)
            and not name.startswith(tag)
        ):
            name = f"{tag}{name}"
        result.append(name)
    return result


def _compose_combination_table(
    combo: TableCombination,
    inputs: list[TableInfo],
    registry: dict[str, Any] | None = None,
) -> tuple[TableInfo, int]:
    if not inputs:
        raise ToolError(f"『{combo.name}』に入力難易度表がありません")
    method = combo.method or "plain"
    missing_same = 0
    first = inputs[0]
    levels: dict[str, TableLevel] = {}

    if method == "plain":
        levels = _clone_table_levels(first)
    elif method == "intersect":
        other_sets = [set(table.all_charts()) for table in inputs[1:]]
        for level_name, level in first.levels.items():
            charts = {
                key: chart for key, chart in level.charts.items()
                if all(key in chart_set for chart_set in other_sets)
            }
            levels[level_name] = TableLevel(level_name, charts)
    elif method == "exclude_others":
        excluded: set[str] = set()
        for table in inputs[1:]:
            excluded.update(table.all_charts())
        for level_name, level in first.levels.items():
            charts = {
                key: chart for key, chart in level.charts.items()
                if key not in excluded
            }
            levels[level_name] = TableLevel(level_name, charts)
    elif method == "union_same":
        for level_name, level in first.levels.items():
            charts = dict(level.charts)
            for table in inputs[1:]:
                same = same_number_level_charts(
                    table, level_name, base_table=first, registry=registry
                )
                if not same:
                    missing_same += 1
                merge_chart_maps(charts, same)
            levels[level_name] = TableLevel(level_name, charts)
    elif method == "merge_same":
        order: list[str] = []
        display_names: dict[str, str] = {}
        grouped: dict[str, dict[str, ChartRef]] = {}
        for table in inputs:
            for level_name, level in table.levels.items():
                number = extract_level_number(level_name)
                key = f"number:{number}" if number is not None else f"name:{level_name.casefold()}"
                if key not in grouped:
                    order.append(key)
                    display_names[key] = level_name
                    grouped[key] = {}
                merge_chart_maps(grouped[key], level.charts)
        levels = {
            display_names[key]: TableLevel(display_names[key], grouped[key])
            for key in order
        }
    elif method == "level_signature":
        # 全入力の和集合を対象に、譜面ごとの所属レベル構成を1つのフォルダ名にする。
        # 例: Ude12と腕0の双方に所属する譜面は「Ude12,腕0」。
        # 入力順は表示名と並び順の双方へ反映する。
        memberships_by_table: list[dict[str, list[tuple[int, str]]]] = []
        all_charts: dict[str, ChartRef] = {}
        for table in inputs:
            membership: dict[str, list[tuple[int, str]]] = {}
            for level_index, (level_name, level) in enumerate(table.levels.items()):
                for chart_key, chart in level.charts.items():
                    membership.setdefault(chart_key, []).append((level_index, level_name))
                    if chart_key not in all_charts:
                        all_charts[chart_key] = chart
            memberships_by_table.append(membership)

        grouped_by_signature: dict[tuple[tuple[str, ...], ...], dict[str, ChartRef]] = {}
        signature_sort_keys: dict[tuple[tuple[str, ...], ...], tuple[int, ...]] = {}
        signature_display_names: dict[tuple[tuple[str, ...], ...], str] = {}

        for chart_key, chart in all_charts.items():
            signature_parts: list[tuple[str, ...]] = []
            display_parts: list[str] = []
            sort_parts: list[int] = []
            for table_index, (table, membership) in enumerate(zip(inputs, memberships_by_table)):
                entries = membership.get(chart_key, [])
                level_names = tuple(level_name for _index, level_name in entries)
                signature_parts.append(level_names)
                display_parts.extend(_qualified_level_names(table, level_names))
                if entries:
                    sort_parts.append(min(index for index, _name in entries))
                elif table_index == 0:
                    # 先頭表に属さない譜面は、先頭表の全レベルの後へ送る。
                    sort_parts.append(len(table.levels) + 1000)
                else:
                    # 同じ先頭レベル内では、単独所属を複合所属より先に置く。
                    sort_parts.append(-1)

            signature = tuple(signature_parts)
            if not display_parts:
                continue
            grouped_by_signature.setdefault(signature, {})[chart_key] = chart
            signature_sort_keys.setdefault(signature, tuple(sort_parts))
            signature_display_names.setdefault(signature, ",".join(display_parts))

        ordered_signatures = sorted(
            grouped_by_signature,
            key=lambda signature: (
                signature_sort_keys[signature],
                signature_display_names[signature].casefold(),
            ),
        )
        levels = {
            signature_display_names[signature]: TableLevel(
                signature_display_names[signature], grouped_by_signature[signature]
            )
            for signature in ordered_signatures
        }
    else:
        raise ToolError(f"未対応の掛け合わせ方です: {method}")

    return TableInfo(
        table_id=combination_source_ref(combo.combination_id),
        url=f"bmscf://combination/{combo.combination_id}",
        name=combo.name,
        tag=first.tag,
        source="generated",
        levels=levels,
    ), missing_same


def resolve_combination_tables(
    tables: list[TableInfo],
    combinations: Iterable[TableCombination],
    difficulty_registry: dict[str, Any] | None = None,
) -> tuple[dict[str, TableInfo], int]:
    source_tables = {table.table_id: table for table in tables if not table.error and table.levels}
    combos = {combo.combination_id: combo for combo in combinations if combo.combination_id}
    registry = difficulty_registry
    if registry is None and any(combo.method == "union_same" for combo in combos.values()):
        registry = load_difficulty_scale_registry()
    validate_combination_graph(combos.values())
    resolved: dict[str, TableInfo] = {}
    state: dict[str, int] = {}
    missing_same = 0

    def resolve_ref(ref: str, owner: TableCombination) -> TableInfo:
        kind, value = split_source_ref(ref)
        if kind == "table":
            table = source_tables.get(value)
            if table is None:
                raise ToolError(f"『{owner.name}』の入力難易度表を読み込めません: {value}")
            return table
        dependency = combos.get(value)
        if dependency is None:
            raise ToolError(f"『{owner.name}』の入力となる生成済み表がありません: {value}")
        return resolve_combo(value)

    def resolve_combo(combo_id: str) -> TableInfo:
        nonlocal missing_same
        if combo_id in resolved:
            return resolved[combo_id]
        if state.get(combo_id) == 1:
            raise ToolError("掛け合わせ済み難易度表が循環参照しています")
        combo = combos[combo_id]
        state[combo_id] = 1
        refs = combination_input_refs(combo)
        required = 1 if combo.method == "plain" else 2
        if len(refs) < required:
            raise ToolError(
                f"『{combo.name}』は『{combination_method_label(combo.method)}』に必要な入力数を満たしていません"
            )
        input_tables = [resolve_ref(ref, combo) for ref in refs]
        table, missing = _compose_combination_table(combo, input_tables, registry=registry)
        missing_same += missing
        resolved[combo_id] = table
        state[combo_id] = 2
        return table

    root_ids = [combo.combination_id for combo in combos.values() if combo.visible]
    for combo_id in root_ids:
        resolve_combo(combo_id)
    return resolved, missing_same


def build_views(
    tables: list[TableInfo],
    base_table_ids: list[str],
    cross_table_ids: list[str],
    presets: list[Preset],
    songs_by_sha: dict[str, dict[str, Any]],
    songs_by_md5: dict[str, dict[str, Any]],
    scores: dict[str, dict[str, Any]],
    analyses: dict[str, dict[str, Any]] | None = None,
    progress: ItemProgressCallback | None = None,
    combinations: list[TableCombination] | None = None,
    difficulty_registry: dict[str, Any] | None = None,
) -> tuple[list[ViewDefinition], int]:
    visible_presets = [p for p in presets if p.visible]
    all_preset = Preset(preset_id="__all__", name="すべて", visible=True, join="AND", conditions=[])
    views: list[ViewDefinition] = []

    profiles: list[TableCombination] = []
    if combinations is not None:
        profiles = list(combinations)
    else:
        # 旧API互換。
        by_id = {t.table_id: t for t in tables if not t.error and t.levels}
        for base_id in base_table_ids:
            base = by_id.get(base_id)
            if not base:
                continue
            profiles.append(TableCombination(
                combination_id=f"legacy:{base_id}:plain", name=base.name,
                input_refs=[table_source_ref(base_id)], base_table_id=base_id, method="plain",
            ))
            for cross_id in cross_table_ids:
                if cross_id == base_id or cross_id not in by_id:
                    continue
                cross = by_id[cross_id]
                refs = [table_source_ref(base_id), table_source_ref(cross_id)]
                profiles.append(TableCombination(
                    combination_id=f"legacy:{base_id}:intersect:{cross_id}",
                    name=f"{base.name} / {cross.name}と共通", input_refs=refs,
                    base_table_id=base_id, method="intersect", cross_table_id=cross_id,
                ))
                profiles.append(TableCombination(
                    combination_id=f"legacy:{base_id}:union_same:{cross_id}",
                    name=f"{base.name} / {cross.name}の同レベルも表示", input_refs=refs,
                    base_table_id=base_id, method="union_same", cross_table_id=cross_id,
                ))

    resolved, missing_same = resolve_combination_tables(
        tables, profiles, difficulty_registry=difficulty_registry
    )
    visible_profiles = [profile for profile in profiles if profile.visible]
    total_levels = sum(len(resolved[profile.combination_id].levels) for profile in visible_profiles)
    processed_levels = 0
    if progress:
        progress(0, max(total_levels, 1), f"{len(visible_profiles):,}種類の掛け合わせ表を構成")

    for profile in visible_profiles:
        result_table = resolved[profile.combination_id]
        refs = combination_input_refs(profile)
        first_kind, first_value = split_source_ref(refs[0]) if refs else ("", "")
        is_instant_profile = (
            profile.combination_id == "builtin:instant-density"
            or (profile.method == "plain" and first_kind == "table" and first_value == INSTANT_TABLE_ID)
        )
        base_presets = (
            [preset for preset in visible_presets if not is_maker_preset(preset)]
            if is_instant_profile
            else visible_presets
        )
        filter_list = [all_preset] + base_presets
        refs_key = ",".join(refs)
        for level_name, level in result_table.levels.items():
            if progress:
                progress(processed_levels, max(total_levels, 1), f"{profile.name} / {level_name}")
            base_view_id = stable_base_view_id(profile.combination_id, level_name, profile.method, refs_key)
            for preset in filter_list:
                views.append(ViewDefinition(
                    view_id=stable_view_id(profile.combination_id, level_name, profile.method, refs_key, preset.preset_id),
                    base_view_id=base_view_id,
                    base_table_id=result_table.table_id,
                    base_table_name=result_table.name,
                    level_name=level_name,
                    combo_key=profile.method or "plain",
                    combo_name=profile.name,
                    cross_table_id=refs_key,
                    preset_id=preset.preset_id,
                    preset_name=preset.name,
                    charts=level.charts,
                    preset=None if preset.preset_id == "__all__" else preset,
                    preset_group_name="" if preset.preset_id == "__all__" else preset.group_name.strip(),
                    profile_id=profile.combination_id,
                    profile_name=profile.name,
                ))
            processed_levels += 1
    if progress:
        progress(max(total_levels, processed_levels, 1), max(total_levels, processed_levels, 1), f"末端フォルダ {len(views):,}個")
    return views, missing_same

def _format_band_number(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else (f"{value:g}")


def make_rhythm_bpm_presets(
    cutoffs: Iterable[float],
    group_16: str = "16分主体",
    group_12: str = "12分主体",
) -> list[Preset]:
    """16分/12分 × 実質BPM帯のグループ付きプリセットを生成する。"""
    values = [float(v) for v in cutoffs]
    if not values:
        raise ToolError("実質BPMの区切りを1つ以上指定してください")
    if any(v <= 0 for v in values):
        raise ToolError("実質BPMの区切りは0より大きい値にしてください")
    if values != sorted(set(values)):
        raise ToolError("実質BPMの区切りは重複なしの昇順で指定してください")
    group_16 = group_16.strip()
    group_12 = group_12.strip()
    if not group_16 or not group_12:
        raise ToolError("16分主体・12分主体の親グループ名を入力してください")

    bands: list[tuple[str, list[tuple[str, str]]]] = []
    first = values[0]
    bands.append((f"実質BPM {_format_band_number(first)}未満", [("<", _format_band_number(first))]))
    for low, high in zip(values, values[1:]):
        if float(low).is_integer() and float(high).is_integer():
            label = f"実質BPM {int(low)}～{int(high) - 1}"
        else:
            label = f"実質BPM {_format_band_number(low)}以上{_format_band_number(high)}未満"
        bands.append((label, [(">=", _format_band_number(low)), ("<", _format_band_number(high))]))
    last = values[-1]
    bands.append((f"実質BPM {_format_band_number(last)}以上", [(">=", _format_band_number(last))]))

    result: list[Preset] = []
    for family, group_name in (("16分系", group_16), ("12分系", group_12)):
        for label, bpm_conditions in bands:
            conditions = [Condition(field="rhythm_family", op="=", value=family)]
            conditions.extend(Condition(field="effective_bpm", op=op, value=value) for op, value in bpm_conditions)
            result.append(Preset(
                name=label,
                visible=True,
                join="AND",
                conditions=conditions,
                group_name=group_name,
            ))
    return result


def sql_escape(value: str) -> str:
    return value.replace("'", "''")


def _condition_subquery(condition: Condition, column: str, default_sql: str = "NULL") -> str:
    cid = sql_escape(condition.condition_id)
    return (
        f"COALESCE((SELECT {column} FROM bmscf_filter_condition "
        f"WHERE condition_id = '{cid}'), {default_sql})"
    )


def _numeric_value_for_condition(condition: Condition) -> float:
    field_name = condition.field
    value = condition.value
    if FIELD_DEFS.get(field_name, {}).get("type") == "bool":
        return 1.0 if parse_bool(value) else 0.0
    if field_name == "clear":
        expected = CLEAR_TYPES.get(str(value).strip().upper())
        return float(expected if expected is not None else int(float(value)))
    if field_name == "best_rank":
        key = str(value).strip().upper()
        if key not in RANK_VALUES:
            raise ToolError(f"ランクが不正です: {value}")
        return float(RANK_VALUES[key])
    if field_name == "rhythm_family":
        return 16.0 if str(value) == "16分系" else 12.0
    if field_name in {"attr", "attr_any"}:
        label = str(value).strip()
        if label == "中速厚め":  # 旧プリセット互換: 16分乱打へ移行
            return float(ATTR_CODES["16分乱打"])
        return float(ATTR_CODES.get(label, ATTR_CODE_UNDECIDED))
    if field_name == "attr_sub":
        label = str(value).strip()
        if label in {"副分類なし", ""}:
            return float(SUBCAT_NONE)
        return float(SUBCAT_CODES.get(label, SUBCAT_NONE))
    if FIELD_DEFS.get(field_name, {}).get("type") in {"int", "float"}:
        return float(value)
    return 0.0


def filter_condition_rows(presets: Iterable[Preset]) -> list[tuple[str, str, int, str, float]]:
    rows: list[tuple[str, str, int, str, float]] = []
    seen: set[str] = set()
    for preset in presets:
        for condition in preset.conditions:
            if not condition.condition_id:
                condition.condition_id = uuid.uuid4().hex
            if condition.condition_id in seen:
                condition.condition_id = uuid.uuid4().hex
            seen.add(condition.condition_id)
            rows.append((
                condition.condition_id,
                preset.preset_id,
                1 if condition.enabled else 0,
                str(condition.value),
                _numeric_value_for_condition(condition),
            ))
    return rows


def _score_rate_sql() -> str:
    exscore = "(COALESCE(score.epg,0)*2 + COALESCE(score.lpg,0)*2 + COALESCE(score.egr,0) + COALESCE(score.lgr,0))"
    notes = "COALESCE(NULLIF(score.notes,0), NULLIF(song.notes,0), 0)"
    return f"(CASE WHEN {notes} > 0 THEN ({exscore} * 50.0 / {notes}) ELSE 0.0 END)"


def _rank_sql() -> str:
    rate = _score_rate_sql()
    return (
        "(CASE "
        "WHEN COALESCE(score.playcount,0) <= 0 THEN -1 "
        f"WHEN {rate} >= 99.999999 THEN 8 "
        f"WHEN {rate} >= {(8.0 / 9.0) * 100.0:.12f} THEN 7 "
        f"WHEN {rate} >= {(7.0 / 9.0) * 100.0:.12f} THEN 6 "
        f"WHEN {rate} >= {(6.0 / 9.0) * 100.0:.12f} THEN 5 "
        f"WHEN {rate} >= {(5.0 / 9.0) * 100.0:.12f} THEN 4 "
        f"WHEN {rate} >= {(4.0 / 9.0) * 100.0:.12f} THEN 3 "
        f"WHEN {rate} >= {(3.0 / 9.0) * 100.0:.12f} THEN 2 "
        f"WHEN {rate} >= {(2.0 / 9.0) * 100.0:.12f} THEN 1 "
        "ELSE 0 END)"
    )


def _comparison_sql(actual: str, op: str, expected: str) -> str:
    if op not in {"=", "!=", ">=", "<=", ">", "<"}:
        raise ToolError(f"SQL化できない比較演算子です: {op}")
    return f"({actual} {op} {expected})"


def condition_predicate_sql(condition: Condition, base_view_id: str) -> str:
    field_name = condition.field
    op = condition.op
    value_num = _condition_subquery(condition, "value_num", "0")
    value_text = _condition_subquery(condition, "value_text", "''")
    if field_name == "played":
        # rian CommandBar adds `AND score.mode = ...` to every custom SQL that
        # contains the literal text `score.`.  That turns a LEFT JOIN into an
        # effective INNER JOIN and removes every genuinely unplayed chart.
        # Query the attached score DB through a short alias instead.  This also
        # matches the pre-v0.3 behavior, which treated a chart as played when it
        # had a play record in any score mode.
        actual = (
            "(CASE WHEN EXISTS (SELECT 1 FROM scoredb.score AS bs "
            "WHERE lower(bs.sha256) = lower(song.sha256) "
            "AND COALESCE(bs.playcount,0) > 0) THEN 1 ELSE 0 END)"
        )
        return _comparison_sql(actual, op, value_num)
    if field_name == "clear":
        return _comparison_sql("COALESCE(score.clear,0)", op, value_num)
    if field_name == "best_rank":
        return _comparison_sql(_rank_sql(), op, value_num)
    if field_name == "minbp":
        valid = "(score.minbp IS NOT NULL AND score.minbp < 2000000000)"
        return f"({valid} AND {_comparison_sql('score.minbp', op, value_num)})"
    if field_name == "miss_rate":
        notes = "COALESCE(NULLIF(song.notes,0), NULLIF(score.notes,0), 0)"
        actual = f"(score.minbp * 100.0 / {notes})"
        valid = f"(score.minbp IS NOT NULL AND score.minbp < 2000000000 AND {notes} > 0)"
        return f"({valid} AND {_comparison_sql(actual, op, value_num)})"
    if field_name == "playcount":
        return _comparison_sql("COALESCE(score.playcount,0)", op, value_num)
    if field_name == "last_play_days":
        actual = (
            "(CASE WHEN COALESCE(score.playcount,0) <= 0 THEN NULL "
            "WHEN COALESCE(score.date,0) > 0 THEN "
            "CAST(MAX(0, CAST(strftime('%s','now') AS INTEGER) - score.date) / 86400 AS INTEGER) "
            "ELSE 999999 END)"
        )
        return _comparison_sql(actual, op, value_num)
    if field_name == "score_rate":
        return _comparison_sql(_score_rate_sql(), op, value_num)
    if field_name == "density_rank":
        base_id = sql_escape(base_view_id)
        return (
            "EXISTS (SELECT 1 FROM bmscf_density_membership AS d "
            f"WHERE d.base_view_id = '{base_id}' AND d.group_name = {value_text} AND "
            "((d.hash_type = 'sha256' AND d.hash = lower(song.sha256)) "
            "OR (d.hash_type = 'md5' AND d.hash = lower(song.md5))))"
        )
    analysis_numeric_columns = {
        "rhythm_family": "rhythm_family",
        "effective_bpm": "effective_bpm",
        "attr": "attr",
        "attr_sub": "attr_sub",
        "practice_low": "practice_low",
        "attr_conf": "attr_conf",
        "grid_bpm": "grid_bpm",
        "avg_chord": "avg_chord",
        "micro_rate": "micro_rate",
        "subgrid_rate": "subgrid_rate",
        "stream_sec": "stream_sec",
        "d10": "d10",
        "d20": "d20",
        "last_kill": "last_kill",
        "recovery_sec": "recovery_sec",
    }
    if field_name in analysis_numeric_columns:
        # Use the same EXISTS-style lookup as base membership.  rian reliably
        # handles this form, while scalar SELECT expressions inside comparisons
        # have produced empty custom folders in the real runtime despite being
        # valid in standalone SQLite.
        column = analysis_numeric_columns[field_name]
        cid = sql_escape(condition.condition_id)
        return (
            "EXISTS (SELECT 1 FROM bmscf_chart_analysis AS a "
            "INNER JOIN bmscf_filter_condition AS c "
            f"ON c.condition_id = '{cid}' "
            "WHERE a.sha256 = lower(song.sha256) "
            "AND COALESCE(a.error,'') = '' "
            f"AND a.{column} IS NOT NULL "
            f"AND (a.{column} {op} c.value_num))"
        )
    if field_name == "attr_any":
        cid = sql_escape(condition.condition_id)
        return (
            "EXISTS (SELECT 1 FROM bmscf_chart_analysis AS a "
            "INNER JOIN bmscf_filter_condition AS c "
            f"ON c.condition_id = '{cid}' "
            "WHERE a.sha256 = lower(song.sha256) "
            "AND COALESCE(a.error,'') = '' "
            "AND a.attr IS NOT NULL "
            "AND (a.attr = c.value_num OR COALESCE(a.attr2, -1) = c.value_num))"
        )
    if field_name in {"maker_has_near_higher", "maker_has_near_lower"}:
        has_column = "has_higher" if field_name.endswith("higher") else "has_lower"
        gap_column = "upper_gap" if field_name.endswith("higher") else "lower_gap"
        cid = sql_escape(condition.condition_id)
        actual = (
            f"(CASE WHEN m.{has_column}=1 AND m.{gap_column}>0 "
            f"AND m.{gap_column}<=3 THEN 1 ELSE 0 END)"
        )
        return (
            "EXISTS (SELECT 1 FROM bmscf_maker_metric AS m "
            "INNER JOIN bmscf_filter_condition AS c "
            f"ON c.condition_id = '{cid}' "
            "WHERE ((m.hash_type = 'sha256' AND m.hash = lower(song.sha256)) "
            "OR (m.hash_type = 'md5' AND m.hash = lower(song.md5))) "
            f"AND ({actual} {op} c.value_num))"
        )
    if field_name in MAKER_METRIC_COLUMNS:
        column = MAKER_METRIC_COLUMNS[field_name]
        cid = sql_escape(condition.condition_id)
        return (
            "EXISTS (SELECT 1 FROM bmscf_maker_metric AS m "
            "INNER JOIN bmscf_filter_condition AS c "
            f"ON c.condition_id = '{cid}' "
            "WHERE ((m.hash_type = 'sha256' AND m.hash = lower(song.sha256)) "
            "OR (m.hash_type = 'md5' AND m.hash = lower(song.md5))) "
            f"AND (m.{column} {op} c.value_num))"
        )
    if field_name in {"song_level", "notes", "minbpm", "maxbpm"}:
        column = "level" if field_name == "song_level" else field_name
        return _comparison_sql(f"COALESCE(song.{column},0)", op, value_num)
    if field_name in {"title", "artist"}:
        actual = f"COALESCE(song.{field_name},'')"
        if op == "含む":
            return f"(instr(lower({actual}), lower({value_text})) > 0)"
        if op == "含まない":
            return f"(instr(lower({actual}), lower({value_text})) = 0)"
        if op in {"=", "!="}:
            return _comparison_sql(f"lower({actual})", op, f"lower({value_text})")
    raise ToolError(f"未対応のフィルター項目です: {field_name}")


def preset_sql(preset: Preset | None, base_view_id: str) -> str:
    if preset is None or not preset.conditions:
        return "1"
    enabled_exprs = [
        _condition_subquery(condition, "enabled", "-1")
        for condition in preset.conditions
    ]
    predicates = [
        condition_predicate_sql(condition, base_view_id)
        for condition in preset.conditions
    ]
    if preset.join.upper() == "OR":
        preset_id = sql_escape(preset.preset_id)
        no_enabled = (
            "(EXISTS (SELECT 1 FROM bmscf_filter_condition "
            f"WHERE preset_id = '{preset_id}') AND "
            "NOT EXISTS (SELECT 1 FROM bmscf_filter_condition "
            f"WHERE preset_id = '{preset_id}' AND enabled = 1))"
        )
        active_terms = [f"(({enabled}) = 1 AND ({predicate}))" for enabled, predicate in zip(enabled_exprs, predicates)]
        return f"(({no_enabled}) OR ({' OR '.join(active_terms)}))"
    active_terms = [
        f"(({enabled}) = 0 OR (({enabled}) = 1 AND ({predicate})))"
        for enabled, predicate in zip(enabled_exprs, predicates)
    ]
    return f"({' AND '.join(active_terms)})"


def base_membership_sql(base_view_id: str) -> str:
    escaped = sql_escape(base_view_id)
    return (
        "EXISTS (SELECT 1 FROM bmscf_base_membership AS bmscf "
        f"WHERE bmscf.base_view_id = '{escaped}' AND "
        "((bmscf.hash_type = 'sha256' AND bmscf.hash = lower(song.sha256)) "
        "OR (bmscf.hash_type = 'md5' AND bmscf.hash = lower(song.md5))))"
    )


def leaf_sql(view: ViewDefinition) -> str:
    return f"({base_membership_sql(view.base_view_id)}) AND ({preset_sql(view.preset, view.base_view_id)})"


def _format_random_course_name(
    template: str,
    level_name: str,
    count: int,
    filter_name: str = "",
    group_name: str = "",
) -> str:
    text = (template or "ALL RANDOM {count}").strip() or "ALL RANDOM {count}"
    try:
        return text.format(
            level=level_name, count=count, filter=filter_name, group=group_name,
        )
    except (KeyError, ValueError):
        return text


def _build_random_course(
    view: ViewDefinition | None,
    requested_stages: int,
    distinct: bool,
    name_template: str,
    level_name: str,
    filter_name: str = "",
    group_name: str = "",
    extra_sql: str = "",
    charts_override: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    charts = charts_override if charts_override is not None else (view.charts if view else None)
    if view is None or not charts:
        return None
    stage_count = min(requested_stages, len(charts))
    if stage_count < 1:
        return None
    sql = leaf_sql(view)
    if extra_sql:
        sql = f"({sql}) AND ({extra_sql})"
    course: dict[str, Any] = {
        "name": _format_random_course_name(
            name_template, level_name, stage_count, filter_name, group_name,
        ),
        "stage": [
            {"title": f"STAGE {i + 1}", "sql": sql if i == 0 else ""}
            for i in range(stage_count)
        ],
    }
    if distinct:
        course["rconstraint"] = ["DISTINCT"]
    return course


# 属性判定は既存フィルタープリセットからのみフォルダ化する。
# v0.9.8で専用の属性階層自動生成を削除した。

def build_folder_tree(
    views: list[ViewDefinition],
    root_name: str = MANAGED_ROOT_NAME,
    progress: ItemProgressCallback | None = None,
    random_course_enabled: bool = False,
    random_course_filter_enabled: bool = False,
    random_course_stages: int = 4,
    random_course_distinct: bool = True,
    random_course_folder_name: str = "ランダムコース",
    random_course_name: str = "ALL RANDOM {count}",
    random_course_filter_name: str = "{filter} RANDOM {count}",
    analyses: dict[str, dict[str, Any]] | None = None,
    attr_folder_enabled: bool = False,
) -> list[dict[str, Any]]:
    """掛け合わせ済み難易度表をdefault.json直下へ置く。root_nameは旧API互換。

    ランダムコースは専用の子フォルダへ隔離する。これによりbeatorajaが
    生成済みコース履歴を同じContainerBarへ追記しても、通常のフィルター一覧を汚さない。
    """
    profile_order: list[str] = []
    profile_nodes: dict[str, dict[str, Any]] = {}
    total = len(views)
    report_every = max(1, total // 250) if total else 1
    if progress:
        progress(0, max(total, 1), "default.json用の階層を構築")
    for index, view in enumerate(views, start=1):
        profile_key = view.profile_id or f"{view.base_table_id}:{view.combo_key}:{view.cross_table_id}"
        profile_name = view.profile_name or view.combo_name or view.base_table_name
        if profile_key not in profile_nodes:
            profile_order.append(profile_key)
            profile_nodes[profile_key] = {"name": profile_name, "levels": {}, "level_order": []}
        pnode = profile_nodes[profile_key]
        if view.level_name not in pnode["levels"]:
            pnode["level_order"].append(view.level_name)
            pnode["levels"][view.level_name] = {
                "items": [], "groups": {}, "all_view": None, "ungrouped_views": [],
            }
        lnode = pnode["levels"][view.level_name]
        if view.preset_id == "__all__":
            lnode["all_view"] = view
        leaf = {"name": view.preset_name, "sql": leaf_sql(view)}
        group_name = (view.preset_group_name or "").strip()
        if not group_name:
            lnode["items"].append(leaf)
            if view.preset_id != "__all__":
                lnode["ungrouped_views"].append(view)
        else:
            group_node = lnode["groups"].get(group_name)
            if group_node is None:
                group_node = {"name": group_name, "folder": [], "course_views": []}
                lnode["groups"][group_name] = group_node
                lnode["items"].append(group_node)
            group_node["folder"].append(leaf)
            group_node["course_views"].append(view)
        if progress and (index == total or index % report_every == 0):
            progress(index, max(total, 1), f"階層 {index:,}/{total:,}")

    roots: list[dict[str, Any]] = []
    requested_stages = max(1, min(20, int(random_course_stages or 1)))
    course_folder_name = (random_course_folder_name or "ランダムコース").strip() or "ランダムコース"
    for profile_key in profile_order:
        pnode = profile_nodes[profile_key]
        level_folders: list[dict[str, Any]] = []
        for level_name in pnode["level_order"]:
            lnode = pnode["levels"][level_name]

            # 各フィルターグループ内へ、そのグループ専用のランダムコース格納先を追加。
            # 生成済みコース履歴はこの子フォルダだけへ蓄積される。
            for group_name, group_node in lnode["groups"].items():
                courses: list[dict[str, Any]] = []
                if random_course_filter_enabled:
                    for filter_view in group_node.get("course_views", []):
                        course = _build_random_course(
                            filter_view, requested_stages, random_course_distinct,
                            random_course_filter_name, level_name,
                            filter_view.preset_name, group_name,
                        )
                        if course is not None:
                            courses.append(course)
                group_node["all_filter_views"] = list(group_node.get("course_views", []))
                group_node.pop("course_views", None)
                if courses:
                    group_node["folder"].append({
                        "name": course_folder_name, "rcourse": courses,
                    })

            # 属性・副分類も通常のフィルタープリセットとしてのみ生成する。
            # analyses / attr_folder_enabled は旧API互換のため引数を残すが、
            # 専用の自動階層は追加しない。

            # レベル直下ではALLと、親グループを持たないフィルターのコースをまとめる。
            level_courses: list[dict[str, Any]] = []
            if random_course_enabled:
                course = _build_random_course(
                    lnode.get("all_view"), requested_stages, random_course_distinct,
                    random_course_name, level_name, "すべて", "",
                )
                if course is not None:
                    level_courses.append(course)
            if random_course_filter_enabled:
                for filter_view in lnode.get("ungrouped_views", []):
                    course = _build_random_course(
                        filter_view, requested_stages, random_course_distinct,
                        random_course_filter_name, level_name,
                        filter_view.preset_name, "",
                    )
                    if course is not None:
                        level_courses.append(course)
            if level_courses:
                lnode["items"].append({
                    "name": course_folder_name, "rcourse": level_courses,
                })

            for group_node in lnode["groups"].values():
                group_node.pop("all_filter_views", None)
            level_folders.append({"name": level_name, "folder": lnode["items"]})
        roots.append({"name": pnode["name"], "folder": level_folders})
    if progress:
        progress(max(total, 1), max(total, 1), "JSON階層の構築完了")
    return roots

def merge_managed_root(default_json_path: Path, managed_root: dict[str, Any], root_name: str) -> Path | None:
    if default_json_path.exists():
        data = safe_load_json(default_json_path, [])
        if not isinstance(data, list):
            raise ToolError(f"folder/default.json のルートが配列ではありません: {default_json_path}")
    else:
        data = []
    backup = backup_file(default_json_path, "bmscf") if default_json_path.exists() else None
    new_data = [x for x in data if not (isinstance(x, dict) and x.get("name") == root_name)]
    new_data.append(managed_root)
    atomic_write_json(default_json_path, new_data)
    return backup


def membership_rows(
    views: list[ViewDefinition],
    progress: ItemProgressCallback | None = None,
) -> list[tuple[str, str, str]]:
    rows: set[tuple[str, str, str]] = set()
    unique_views: list[ViewDefinition] = []
    handled: set[str] = set()
    for view in views:
        if view.base_view_id not in handled:
            handled.add(view.base_view_id)
            unique_views.append(view)
    total = len(unique_views)
    if progress:
        progress(0, max(total, 1), "基礎所属を作成")
    for index, view in enumerate(unique_views, start=1):
        for chart in view.charts.values():
            for ref in chart.hash_refs():
                if ref.value:
                    rows.add((view.base_view_id, ref.hash_type, ref.value.lower()))
        if progress:
            progress(index, max(total, 1), f"基礎所属 {index:,}/{total:,}")
    return sorted(rows)


def density_membership_rows(
    views: list[ViewDefinition],
    songs_by_sha: dict[str, dict[str, Any]],
    songs_by_md5: dict[str, dict[str, Any]],
    progress: ItemProgressCallback | None = None,
) -> list[tuple[str, str, str, str]]:
    rows: set[tuple[str, str, str, str]] = set()
    unique_views: list[ViewDefinition] = []
    handled: set[str] = set()
    for view in views:
        if view.base_view_id not in handled:
            handled.add(view.base_view_id)
            unique_views.append(view)
    total = len(unique_views)
    if progress:
        progress(0, max(total, 1), "密度順位所属を作成")
    for index, view in enumerate(unique_views, start=1):
        groups_by_key = density_rank_groups(view.charts, songs_by_sha, songs_by_md5)
        for key, groups in groups_by_key.items():
            chart = view.charts.get(key)
            if not chart:
                continue
            for group_name in groups:
                for ref in chart.hash_refs():
                    if ref.value:
                        rows.add((view.base_view_id, group_name, ref.hash_type, ref.value.lower()))
        if progress:
            progress(index, max(total, 1), f"密度所属 {index:,}/{total:,}")
    return sorted(rows)


def analysis_publish_rows(analyses: dict[str, dict[str, Any]]) -> list[tuple[Any, ...]]:
    rows: list[tuple[Any, ...]] = []
    for sha, row in analyses.items():
        rows.append((
            sha.lower(),
            row.get("rhythm_family"),
            row.get("dominant_division"),
            row.get("effective_bpm"),
            int(row.get("analysis_version") or 0),
            str(row.get("error") or ""),
        ) + tuple(row.get(name) for name in _PUBLISHED_ANALYSIS_COLUMNS[6:]))
    return rows


_PUBLISHED_ANALYSIS_COLUMNS: tuple[str, ...] = (
    "sha256", "rhythm_family", "dominant_division", "effective_bpm",
    "analysis_version", "error",
    "attr", "attr2", "attr_sub", "attr_conf", "grid_bpm", "avg_chord", "chord_ge3",
    "micro_rate", "long_jack_rate", "subgrid_rate", "ongrid_rate", "stream_sec",
    "d2", "d10", "d20", "avg_density", "last_kill", "recovery_sec", "total_notes",
    "chart_seconds", "is_dp",
    "full_rate", "full_alt_rate", "micro_thin", "micro_chord", "micro_full",
    "tail_grid_bpm", "tail_chord", "tail_sub", "practice_low",
)


def _migrate_published_analysis(con: sqlite3.Connection) -> None:
    """v0.8.x以前の bmscf_chart_analysis (旧カラム構成) を作り直す。

    派生テーブルのため中身は次回の書込みで全再構築される。旧構成のまま
    残すと列数不一致でINSERTが失敗するので、列が足りなければDROPする。
    """
    rows = con.execute("PRAGMA table_info(bmscf_chart_analysis)").fetchall()
    if rows:
        existing = {row[1] for row in rows}
        if not set(_PUBLISHED_ANALYSIS_COLUMNS).issubset(existing):
            con.execute("DROP TABLE bmscf_chart_analysis")


def _create_support_tables(con: sqlite3.Connection) -> None:
    _migrate_published_analysis(con)
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS bmscf_base_membership (
            base_view_id TEXT NOT NULL,
            hash_type TEXT NOT NULL,
            hash TEXT NOT NULL,
            PRIMARY KEY (base_view_id, hash_type, hash)
        );
        CREATE INDEX IF NOT EXISTS idx_bmscf_base_lookup
            ON bmscf_base_membership(base_view_id, hash_type, hash);
        CREATE INDEX IF NOT EXISTS idx_bmscf_base_hash
            ON bmscf_base_membership(hash_type, hash, base_view_id);

        CREATE TABLE IF NOT EXISTS bmscf_density_membership (
            base_view_id TEXT NOT NULL,
            group_name TEXT NOT NULL,
            hash_type TEXT NOT NULL,
            hash TEXT NOT NULL,
            PRIMARY KEY (base_view_id, group_name, hash_type, hash)
        );
        CREATE INDEX IF NOT EXISTS idx_bmscf_density_lookup
            ON bmscf_density_membership(base_view_id, group_name, hash_type, hash);

        CREATE TABLE IF NOT EXISTS bmscf_chart_analysis (
            sha256 TEXT PRIMARY KEY,
            rhythm_family INTEGER,
            dominant_division INTEGER,
            effective_bpm REAL,
            analysis_version INTEGER NOT NULL DEFAULT 0,
            error TEXT NOT NULL DEFAULT '',
            attr INTEGER,
            attr2 INTEGER,
            attr_sub INTEGER,
            attr_conf REAL,
            grid_bpm REAL,
            avg_chord REAL,
            chord_ge3 REAL,
            micro_rate REAL,
            long_jack_rate REAL,
            subgrid_rate REAL,
            ongrid_rate REAL,
            stream_sec REAL,
            d2 REAL,
            d10 REAL,
            d20 REAL,
            avg_density REAL,
            last_kill REAL,
            recovery_sec REAL,
            total_notes INTEGER,
            chart_seconds REAL,
            is_dp INTEGER,
            full_rate REAL,
            full_alt_rate REAL,
            micro_thin REAL,
            micro_chord REAL,
            micro_full REAL,
            tail_grid_bpm REAL,
            tail_chord REAL,
            tail_sub REAL,
            practice_low INTEGER
        );

        CREATE TABLE IF NOT EXISTS bmscf_maker_metric (
            hash_type TEXT NOT NULL,
            hash TEXT NOT NULL,
            axis_known INTEGER NOT NULL DEFAULT 0,
            material_ok INTEGER NOT NULL DEFAULT 0,
            needs_review INTEGER NOT NULL DEFAULT 0,
            unused_audio_count INTEGER NOT NULL DEFAULT 0,
            chart_count INTEGER NOT NULL DEFAULT 0,
            other_advanced_count INTEGER NOT NULL DEFAULT 0,
            has_higher INTEGER NOT NULL DEFAULT 0,
            has_lower INTEGER NOT NULL DEFAULT 0,
            upper_gap REAL NOT NULL DEFAULT -1,
            lower_gap REAL NOT NULL DEFAULT -1,
            unknown_count INTEGER NOT NULL DEFAULT 0,
            analysis_version INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (hash_type, hash)
        );
        CREATE INDEX IF NOT EXISTS idx_bmscf_maker_lookup
            ON bmscf_maker_metric(hash_type, hash);

        CREATE TABLE IF NOT EXISTS bmscf_filter_condition (
            condition_id TEXT PRIMARY KEY,
            preset_id TEXT NOT NULL,
            enabled INTEGER NOT NULL,
            value_text TEXT NOT NULL,
            value_num REAL NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_bmscf_filter_preset
            ON bmscf_filter_condition(preset_id, enabled);
        """
    )


def ensure_initial_songdata_backup(
    song_db: Path,
    backup_dir: Path,
    timeout_seconds: int = 15,
) -> Path | None:
    """Create one SQLite-consistent backup per songdata.db path.

    The path-derived name intentionally prevents repeated backups on every folder
    update while still protecting a separately selected beatoraja installation.
    """
    if not song_db.exists():
        raise ToolError(f"songdata.db が見つかりません: {song_db}")
    resolved = str(song_db.resolve())
    key = hashlib.sha256(resolved.encode("utf-8", errors="surrogatepass")).hexdigest()[:12]
    backup_dir.mkdir(parents=True, exist_ok=True)
    target = backup_dir / f"songdata_initial_{key}.db"
    if target.exists():
        return None

    temp_target = target.with_suffix(target.suffix + ".tmp")
    temp_target.unlink(missing_ok=True)
    source_con: sqlite3.Connection | None = None
    backup_con: sqlite3.Connection | None = None
    try:
        source_con = sqlite3.connect(str(song_db), timeout=timeout_seconds)
        source_con.execute(f"PRAGMA busy_timeout={timeout_seconds * 1000}")
        source_check = source_con.execute("PRAGMA quick_check").fetchone()
        if not source_check or str(source_check[0]).lower() != "ok":
            raise ToolError("songdata.dbの整合性確認に失敗したため、書込みを中止しました")

        backup_con = sqlite3.connect(str(temp_target), timeout=timeout_seconds)
        source_con.backup(backup_con)
        backup_con.commit()
        backup_check = backup_con.execute("PRAGMA quick_check").fetchone()
        if not backup_check or str(backup_check[0]).lower() != "ok":
            raise ToolError("songdata.dbの初回バックアップ確認に失敗しました")
        backup_con.close()
        backup_con = None
        source_con.close()
        source_con = None
        os.replace(temp_target, target)
        metadata = {
            "source": resolved,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "backup": target.name,
        }
        atomic_write_json(target.with_suffix(".json"), metadata)
        return target
    except sqlite3.Error as exc:
        raise ToolError(f"songdata.dbの初回バックアップに失敗しました。\n{exc}") from exc
    finally:
        if backup_con is not None:
            backup_con.close()
        if source_con is not None:
            source_con.close()
        temp_target.unlink(missing_ok=True)


def write_filter_conditions(
    song_db: Path,
    presets: Iterable[Preset],
    timeout_seconds: int = 15,
) -> int:
    if not song_db.exists():
        raise ToolError(f"songdata.db が見つかりません: {song_db}")
    rows = filter_condition_rows(presets)
    try:
        con = sqlite3.connect(song_db, timeout=timeout_seconds)
        con.execute(f"PRAGMA busy_timeout={timeout_seconds * 1000}")
        _create_support_tables(con)
        con.execute("BEGIN IMMEDIATE")
        con.execute("CREATE TEMP TABLE bmscf_condition_stage(condition_id TEXT PRIMARY KEY, preset_id TEXT, enabled INTEGER, value_text TEXT, value_num REAL) WITHOUT ROWID")
        con.executemany("INSERT INTO bmscf_condition_stage VALUES (?,?,?,?,?)", rows)
        con.execute("DELETE FROM bmscf_filter_condition")
        con.execute("INSERT INTO bmscf_filter_condition SELECT * FROM bmscf_condition_stage")
        con.commit()
        con.close()
        return len(rows)
    except sqlite3.Error as exc:
        try:
            con.rollback()  # type: ignore[name-defined]
            con.close()  # type: ignore[name-defined]
        except Exception:
            pass
        raise ToolError(f"フィルター条件の同期に失敗しました。\n{exc}") from exc


def write_membership_db(
    song_db: Path,
    rows: list[tuple[str, str, str]],
    density_rows: list[tuple[str, str, str, str]] | None = None,
    analysis_rows: list[tuple[Any, ...]] | None = None,
    maker_rows: list[tuple[Any, ...]] | None = None,
    condition_rows: list[tuple[str, str, int, str, float]] | None = None,
    timeout_seconds: int = 15,
    progress: ItemProgressCallback | None = None,
) -> None:
    if not song_db.exists():
        raise ToolError(f"songdata.db が見つかりません: {song_db}")
    try:
        if progress:
            progress(0, 7, "songdata.dbへの書込み準備")
        con = sqlite3.connect(song_db, timeout=timeout_seconds)
        con.execute(f"PRAGMA busy_timeout={timeout_seconds * 1000}")
        _create_support_tables(con)
        con.execute("BEGIN IMMEDIATE")
        con.execute(
            "CREATE TEMP TABLE bmscf_stage ("
            "base_view_id TEXT NOT NULL, hash_type TEXT NOT NULL, hash TEXT NOT NULL, "
            "PRIMARY KEY(base_view_id, hash_type, hash)) WITHOUT ROWID"
        )
        con.executemany(
            "INSERT OR IGNORE INTO bmscf_stage(base_view_id, hash_type, hash) VALUES (?,?,?)",
            rows,
        )
        con.execute("DELETE FROM bmscf_base_membership")
        con.execute(
            "INSERT INTO bmscf_base_membership(base_view_id, hash_type, hash) "
            "SELECT base_view_id, hash_type, hash FROM bmscf_stage"
        )
        if progress:
            progress(1, 7, f"基礎所属 {len(rows):,}行を書込み")
        con.execute("CREATE TEMP TABLE bmscf_density_stage(base_view_id TEXT, group_name TEXT, hash_type TEXT, hash TEXT, PRIMARY KEY(base_view_id, group_name, hash_type, hash)) WITHOUT ROWID")
        con.executemany("INSERT OR IGNORE INTO bmscf_density_stage VALUES (?,?,?,?)", density_rows or [])
        con.execute("DELETE FROM bmscf_density_membership")
        con.execute("INSERT INTO bmscf_density_membership SELECT * FROM bmscf_density_stage")
        if progress:
            progress(2, 7, f"密度所属 {len(density_rows or []):,}行を書込み")

        stage_columns = ", ".join(
            f"{name} TEXT PRIMARY KEY" if name == "sha256" else f"{name}"
            for name in _PUBLISHED_ANALYSIS_COLUMNS
        )
        stage_placeholders = ",".join("?" for _ in _PUBLISHED_ANALYSIS_COLUMNS)
        con.execute(f"CREATE TEMP TABLE bmscf_analysis_stage({stage_columns}) WITHOUT ROWID")
        column_count = len(_PUBLISHED_ANALYSIS_COLUMNS)
        padded_rows = [
            tuple(row) + (None,) * (column_count - len(row))
            for row in (analysis_rows or [])
        ]
        con.executemany(
            f"INSERT OR REPLACE INTO bmscf_analysis_stage VALUES ({stage_placeholders})",
            padded_rows,
        )
        con.execute("DELETE FROM bmscf_chart_analysis")
        con.execute(
            "INSERT INTO bmscf_chart_analysis ({0}) SELECT {0} FROM bmscf_analysis_stage".format(
                ", ".join(_PUBLISHED_ANALYSIS_COLUMNS)
            )
        )
        if progress:
            progress(3, 7, f"解析結果 {len(analysis_rows or []):,}行を書込み")

        con.execute(
            "CREATE TEMP TABLE bmscf_maker_stage("
            "hash_type TEXT, hash TEXT, axis_known INTEGER, material_ok INTEGER, needs_review INTEGER, "
            "unused_audio_count INTEGER, chart_count INTEGER, other_advanced_count INTEGER, "
            "has_higher INTEGER, has_lower INTEGER, upper_gap REAL, lower_gap REAL, "
            "unknown_count INTEGER, analysis_version INTEGER, PRIMARY KEY(hash_type, hash)) WITHOUT ROWID"
        )
        con.executemany("INSERT OR REPLACE INTO bmscf_maker_stage VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", maker_rows or [])
        con.execute("DELETE FROM bmscf_maker_metric")
        con.execute("INSERT INTO bmscf_maker_metric SELECT * FROM bmscf_maker_stage")
        if progress:
            progress(4, 7, f"差分制作指標 {len(maker_rows or []):,}行を書込み")

        con.execute("CREATE TEMP TABLE bmscf_condition_stage(condition_id TEXT PRIMARY KEY, preset_id TEXT, enabled INTEGER, value_text TEXT, value_num REAL) WITHOUT ROWID")
        con.executemany("INSERT OR REPLACE INTO bmscf_condition_stage VALUES (?,?,?,?,?)", condition_rows or [])
        con.execute("DELETE FROM bmscf_filter_condition")
        con.execute("INSERT INTO bmscf_filter_condition SELECT * FROM bmscf_condition_stage")
        if progress:
            progress(5, 7, f"動的条件 {len(condition_rows or []):,}行を書込み")

        if progress:
            progress(6, 7, "トランザクションを確定中")
        con.commit()
        con.close()
        if progress:
            progress(7, 7, "構成DBの書込み完了")
    except sqlite3.Error as exc:
        try:
            con.rollback()  # type: ignore[name-defined]
            con.close()  # type: ignore[name-defined]
        except Exception:
            pass
        raise ToolError(
            "songdata.dbへの所属情報書込みに失敗しました。\n"
            "beatorajaがDB更新中の場合は数秒後に再試行してください。\n"
            f"{exc}"
        ) from exc


def generate_all(
    env: EnvironmentPaths,
    tables: list[TableInfo],
    settings: AppSettings,
    write_json: bool = True,
    analysis_db: Path | None = None,
    update_analysis: bool = False,
    progress_callback: ProgressCallback | None = None,
) -> GenerationResult:
    active_presets = active_filter_presets(settings.presets, settings.maker_table_enabled)
    generation_presets = visible_filter_presets(active_presets)
    active_combinations = generated_combinations(settings)
    selected_table_ids = combination_table_ids(settings.table_combinations, visible_only=True)
    needs_maker_metrics = (
        settings.maker_table_enabled
        and analysis_db is not None
        and bool(selected_table_ids)
        and presets_require_maker_metrics(generation_presets)
    )
    needs_material_analysis = needs_maker_metrics and presets_require_material_analysis(generation_presets)
    stages: list[tuple[str, str, float]] = [
        ("song_db", "楽曲DB読込", 12),
        ("canonicalize", "難易度表照合", 8),
    ]
    if settings.instant_density_table_enabled:
        stages.append(("instant", "表外密度順位", 5))
    if needs_material_analysis:
        stages.append(("material", "対象曲の制作素材解析", 30))
    if needs_maker_metrics:
        stages.append(("maker", "対象曲の差分制作指標", 10))
    if analysis_db is not None and update_analysis and presets_require_chart_analysis(generation_presets):
        stages.append(("chart_analysis", "譜面リズム解析", 18))
    stages.extend([
        ("views", "フォルダー構成作成", 8),
        ("membership", "所属情報作成", 9),
        ("write_db", "構成DB書込み", 8),
    ])
    if write_json:
        stages.append(("json", "default.json更新", 5))
    reporter = ProgressReporter(progress_callback, stages)

    reporter.start("song_db", "songdata.dbを読み込んでいます")
    songs_by_sha, songs_by_md5 = read_song_rows(
        env.song_db,
        progress=lambda c, t, m: reporter.update("song_db", c, t, m),
    )
    reporter.finish("song_db", f"{len(songs_by_sha):,}譜面を読込済み")

    working_tables = [table for table in tables if table.table_id not in {INSTANT_TABLE_ID, MAKER_TABLE_ID}]
    reporter.start("canonicalize", "難易度表のハッシュを楽曲DBと照合")
    canonicalize_table_charts(
        working_tables, songs_by_sha, songs_by_md5,
        progress=lambda c, t, m: reporter.update("canonicalize", c, t, m),
    )
    reporter.finish("canonicalize", "難易度表の照合完了")

    virtual_base_ids: list[str] = []
    instant_chart_count = 0
    maker_folder_count = 0
    maker_chart_count = 0
    maker_lookup: dict[str, dict[str, Any]] = {}
    maker_rows: list[tuple[Any, ...]] = []
    material_stats = {"analyzed": 0, "cached": 0, "failed": 0, "folders": 0}

    if settings.instant_density_table_enabled:
        reporter.start("instant", "表所属済み譜面を除外して密度分類中")
        instant_table = build_instant_density_table(
            working_tables, songs_by_sha, songs_by_md5, load_instant_density_profiles()
        )
        working_tables.append(instant_table)
        virtual_base_ids.append(INSTANT_TABLE_ID)
        instant_chart_count = instant_table.chart_count
        reporter.finish("instant", f"表外譜面 {instant_chart_count:,}件")

    if needs_maker_metrics:
        maker_target_charts = selected_table_charts(
            working_tables,
            selected_table_ids,
        )
        maker_target_folders = target_folder_keys_for_charts(
            env.root, maker_target_charts, songs_by_sha, songs_by_md5
        )
        maker_target_keys = target_chart_keys(maker_target_charts)
        material_rows: dict[str, dict[str, Any]] = {}
        if needs_material_analysis:
            reporter.start(
                "material",
                f"選択表に含まれる {len(maker_target_folders):,}楽曲フォルダだけを解析中",
            )
            material_rows, material_stats = update_song_material_analysis(
                analysis_db, env.root, songs_by_sha, songs_by_md5, force=False,
                progress=lambda c, t, m: reporter.update("material", c, t, m),
                target_folder_keys=maker_target_folders,
            )
            reporter.finish(
                "material",
                f"対象 {int(material_stats.get('folders') or 0):,}フォルダのみ確認",
            )

        reporter.start("maker", "選択表に関係する楽曲だけ差分構成を判定中")
        maker_lookup, maker_rows, maker_stats = build_maker_chart_metrics(
            env.root, songs_by_sha, songs_by_md5, material_rows,
            tables=working_tables, registry=load_difficulty_scale_registry(),
            progress=lambda c, t, m: reporter.update("maker", c, t, m),
            target_folder_keys=maker_target_folders,
            target_keys=maker_target_keys,
        )
        maker_folder_count = int(maker_stats.get("folders") or 0)
        maker_chart_count = int(maker_stats.get("charts") or 0)
        reporter.finish("maker", f"対象 {maker_chart_count:,}譜面へ差分制作指標を付与")

    analysis_stats = {"analyzed": 0, "cached": 0, "missing": 0, "failed": 0}
    if analysis_db is not None and update_analysis and presets_require_chart_analysis(generation_presets):
        charts = selected_table_charts(working_tables, selected_table_ids)
        reporter.start("chart_analysis", f"{len(charts):,}譜面のリズムを確認")
        analysis_stats = update_chart_analysis(
            analysis_db, env.root, charts, songs_by_sha, songs_by_md5, force=False,
            progress=lambda c, t, m: reporter.update("chart_analysis", c, t, m),
        )
        reporter.finish("chart_analysis", "譜面リズム解析完了")

    rhythm_analyses = read_analysis_rows(analysis_db)
    analyses = {key: dict(value) for key, value in rhythm_analyses.items()}
    for key, metric in maker_lookup.items():
        analyses.setdefault(key, {}).update(metric)

    reporter.start("views", "有効フィルターからフォルダーを構成中")
    views, missing_same = build_views(
        tables=working_tables,
        base_table_ids=[],
        cross_table_ids=[],
        presets=generation_presets,
        songs_by_sha=songs_by_sha,
        songs_by_md5=songs_by_md5,
        scores={},
        analyses=analyses,
        progress=lambda c, t, m: reporter.update("views", c, t, m),
        combinations=active_combinations,
    )
    if not views:
        raise ToolError("生成対象がありません。基準難易度表を1件以上選択してください。")
    reporter.finish("views", f"末端フォルダ {len(views):,}個")

    reporter.start("membership", "基礎所属と密度所属を作成中")
    rows = membership_rows(
        views,
        progress=lambda c, t, m: reporter.update("membership", c, max(t * 2, 1), "基礎所属: " + m),
    )
    density_rows = density_membership_rows(
        views, songs_by_sha, songs_by_md5,
        progress=lambda c, t, m: reporter.update("membership", t + c, max(t * 2, 1), "密度所属: " + m),
    )
    published_analysis = analysis_publish_rows(rhythm_analyses)
    condition_rows = filter_condition_rows(generation_presets)
    reporter.finish("membership", f"基礎 {len(rows):,}行 / 密度 {len(density_rows):,}行")

    reporter.start("write_db", "songdata.dbへ構成情報を書込み中")
    write_membership_db(
        env.song_db,
        rows,
        density_rows=density_rows,
        analysis_rows=published_analysis,
        maker_rows=maker_rows,
        condition_rows=condition_rows,
        progress=lambda c, t, m: reporter.update("write_db", c, t, m),
    )
    reporter.finish("write_db", "構成DB更新完了")

    backup = None
    if write_json:
        reporter.start("json", "default.json用の階層を構築中")
        roots = build_folder_tree(
            views, settings.root_folder_name,
            progress=lambda c, t, m: reporter.update("json", c, max(t * 2, 1), m),
            random_course_enabled=settings.random_course_enabled,
            random_course_filter_enabled=settings.random_course_filter_enabled,
            random_course_stages=settings.random_course_stages,
            random_course_distinct=settings.random_course_distinct,
            random_course_folder_name=settings.random_course_folder_name,
            random_course_name=settings.random_course_name,
            random_course_filter_name=settings.random_course_filter_name,
        )
        reporter.update("json", 1, 2, "default.jsonをバックアップして更新中")
        backup = write_default_json_layout(env.default_json, settings, roots)
        reporter.finish("json", "default.json更新完了")

    return GenerationResult(
        view_count=len(views),
        membership_count=len(rows),
        folder_json_path=env.default_json,
        backup_path=backup,
        missing_same_level_count=missing_same,
        empty_view_count=sum(1 for view in views if not view.charts),
        analysis_reclassified=int(analysis_stats.get("reclassified") or 0),
        analysis_analyzed=int(analysis_stats.get("analyzed") or 0),
        analysis_cached=int(analysis_stats.get("cached") or 0),
        analysis_missing=int(analysis_stats.get("missing") or 0),
        analysis_failed=int(analysis_stats.get("failed") or 0),
        condition_count=len(condition_rows),
        analysis_row_count=len(published_analysis),
        instant_chart_count=instant_chart_count,
        maker_folder_count=maker_folder_count,
        maker_chart_count=maker_chart_count,
        material_analyzed=int(material_stats.get("analyzed") or 0),
        material_cached=int(material_stats.get("cached") or 0),
        material_quick_cached=int(material_stats.get("quick_cached") or 0),
        material_full_scans=int(material_stats.get("full_scans") or 0),
        material_failed=int(material_stats.get("failed") or 0),
    )


def analyze_selected_tables(
    env: EnvironmentPaths,
    tables: list[TableInfo],
    settings: AppSettings,
    analysis_db: Path,
    force: bool = False,
    progress_callback: ProgressCallback | None = None,
    cancel_check: CancelCheck | None = None,
) -> dict[str, int]:
    reporter = ProgressReporter(progress_callback, [
        ("song_db", "楽曲DB読込", 20),
        ("canonicalize", "難易度表照合", 10),
        ("chart_analysis", "譜面リズム解析", 70),
    ])

    def check_cancel() -> None:
        if cancel_check and cancel_check():
            raise AnalysisCancelled("差分解析を中止しました")

    def stage_progress(stage_key: str) -> ItemProgressCallback:
        def callback(current: int, total: int, message: str) -> None:
            check_cancel()
            reporter.update(stage_key, current, total, message)
        return callback

    reporter.start("song_db", "songdata.dbを読み込んでいます")
    songs_by_sha, songs_by_md5 = read_song_rows(
        env.song_db,
        progress=stage_progress("song_db"),
    )
    check_cancel()
    reporter.finish("song_db", f"{len(songs_by_sha):,}譜面を読込済み")
    reporter.start("canonicalize", "難易度表を照合中")
    canonicalize_table_charts(
        tables, songs_by_sha, songs_by_md5,
        progress=stage_progress("canonicalize"),
    )
    check_cancel()
    reporter.finish("canonicalize", "難易度表の照合完了")
    charts = selected_table_charts(tables, combination_table_ids(settings.table_combinations, visible_only=True))
    if not charts:
        raise ToolError("解析対象がありません。対象難易度表を1件以上選択してください。")
    reporter.start("chart_analysis", f"{len(charts):,}譜面を解析")
    result = update_chart_analysis(
        analysis_db, env.root, charts, songs_by_sha, songs_by_md5, force=force,
        progress=stage_progress("chart_analysis"),
        cancel_check=cancel_check,
    )
    reporter.finish("chart_analysis", "譜面解析完了")
    return result


def _analysis_row_changed(source_row: dict[str, Any], target_row: dict[str, Any] | None) -> bool:
    if target_row is None:
        return True
    # The pending DB starts as a copy of the main DB. Compare every stored
    # analysis field so new features, errors, classifications, or metadata are
    # never silently left uncommitted.
    return any(source_row.get(name) != target_row.get(name) for name in source_row)


def analysis_db_diff_count(source_db: Path, target_db: Path) -> int:
    """Count staged rows that are new or changed compared with the main DB."""
    if not source_db.exists():
        return 0
    source = _analysis_connect(source_db)
    source_rows = {str(row["sha256"]): dict(row) for row in source.execute("SELECT * FROM chart_analysis")}
    source.close()
    if not target_db.exists():
        return len(source_rows)
    target = _analysis_connect(target_db)
    target_rows = {str(row["sha256"]): dict(row) for row in target.execute("SELECT * FROM chart_analysis")}
    target.close()
    return sum(1 for sha, row in source_rows.items() if _analysis_row_changed(row, target_rows.get(sha)))


def merge_chart_analysis_db(source_db: Path, target_db: Path) -> dict[str, int]:
    """Merge only new or changed staged chart_analysis rows into the main DB."""
    if not source_db.exists():
        return {"source": 0, "merged": 0}
    source = _analysis_connect(source_db)
    source_rows_raw = source.execute("SELECT * FROM chart_analysis").fetchall()
    source_columns = [str(row[1]) for row in source.execute("PRAGMA table_info(chart_analysis)").fetchall()]
    source.close()
    if not source_rows_raw:
        return {"source": 0, "merged": 0}

    source_rows = [dict(row) for row in source_rows_raw]
    target = _analysis_connect(target_db)
    target_columns = {str(row[1]) for row in target.execute("PRAGMA table_info(chart_analysis)").fetchall()}
    target_rows = {str(row["sha256"]): dict(row) for row in target.execute("SELECT * FROM chart_analysis")}
    changed_rows = [row for row in source_rows if _analysis_row_changed(row, target_rows.get(str(row.get("sha256") or "")))]
    if not changed_rows:
        target.close()
        return {"source": len(source_rows), "merged": 0}

    columns = [name for name in source_columns if name in target_columns]
    placeholders = ",".join("?" for _ in columns)
    update_columns = [name for name in columns if name != "sha256"]
    update_sql = ", ".join(f"{name}=excluded.{name}" for name in update_columns)
    sql = (
        f"INSERT INTO chart_analysis ({', '.join(columns)}) VALUES ({placeholders}) "
        f"ON CONFLICT(sha256) DO UPDATE SET {update_sql}"
    )
    values = [tuple(row.get(name) for name in columns) for row in changed_rows]
    target.executemany(sql, values)
    target.commit()
    target.close()
    return {"source": len(source_rows), "merged": len(values)}


def analyze_song_database(
    env: EnvironmentPaths,
    analysis_db: Path,
    force: bool = False,
    progress_callback: ProgressCallback | None = None,
    cancel_check: CancelCheck | None = None,
) -> dict[str, int]:
    """Analyze every chart registered in songdata.db.

    This operation is intentionally independent of selected difficulty tables and
    filter settings.  Cached rows are reused unless ``force`` is requested.
    """
    reporter = ProgressReporter(progress_callback, [
        ("song_db", "楽曲DB読込", 20),
        ("chart_analysis", "差分解析", 80),
    ])

    def check_cancel() -> None:
        if cancel_check and cancel_check():
            raise AnalysisCancelled("差分解析を中止しました")

    def stage_progress(stage_key: str) -> ItemProgressCallback:
        def callback(current: int, total: int, message: str) -> None:
            check_cancel()
            reporter.update(stage_key, current, total, message)
        return callback

    reporter.start("song_db", "songdata.dbを読み込んでいます")
    songs_by_sha, songs_by_md5 = read_song_rows(
        env.song_db,
        progress=stage_progress("song_db"),
    )
    check_cancel()
    reporter.finish("song_db", f"{len(songs_by_sha):,}譜面を読込済み")

    charts: dict[str, ChartRef] = {}
    for sha, song in songs_by_sha.items():
        normalized_sha = str(sha or "").strip().lower()
        if not normalized_sha:
            continue
        charts[f"sha256:{normalized_sha}"] = ChartRef(
            sha256=normalized_sha,
            md5=str(song.get("md5") or "").strip().lower(),
            title=str(song.get("title") or ""),
            artist=str(song.get("artist") or ""),
        )
    if not charts:
        raise ToolError("songdata.dbに解析可能な差分がありません")

    reporter.start("chart_analysis", f"{len(charts):,}差分を解析")
    result = update_chart_analysis(
        analysis_db,
        env.root,
        charts,
        songs_by_sha,
        songs_by_md5,
        force=force,
        progress=stage_progress("chart_analysis"),
        cancel_check=cancel_check,
    )
    reporter.finish("chart_analysis", "差分解析完了")
    return result


def preview_counts(
    env: EnvironmentPaths,
    tables: list[TableInfo],
    settings: AppSettings,
    analysis_db: Path | None = None,
) -> tuple[int, int, int]:
    songs_by_sha, songs_by_md5 = read_song_rows(env.song_db)
    scores = read_scores(env.score_db)
    working_tables = [table for table in tables if table.table_id not in {INSTANT_TABLE_ID, MAKER_TABLE_ID}]
    canonicalize_table_charts(working_tables, songs_by_sha, songs_by_md5)
    virtual_base_ids: list[str] = []
    if settings.instant_density_table_enabled:
        working_tables.append(build_instant_density_table(
            working_tables, songs_by_sha, songs_by_md5, load_instant_density_profiles()
        ))
        virtual_base_ids.append(INSTANT_TABLE_ID)

    analyses = read_analysis_rows(analysis_db)
    active_presets = active_filter_presets(settings.presets, settings.maker_table_enabled)
    generation_presets = visible_filter_presets(active_presets)
    if (
        settings.maker_table_enabled
        and analysis_db is not None
        and bool(combination_table_ids(settings.table_combinations, visible_only=True))
        and presets_require_maker_metrics(generation_presets)
    ):
        maker_target_charts = selected_table_charts(
            working_tables, combination_table_ids(settings.table_combinations, visible_only=True)
        )
        maker_target_folders = target_folder_keys_for_charts(
            env.root, maker_target_charts, songs_by_sha, songs_by_md5
        )
        material_rows = read_song_material_analysis(analysis_db)
        material_rows = {key: value for key, value in material_rows.items() if key in maker_target_folders}
        maker_lookup, _maker_rows, _maker_stats = build_maker_chart_metrics(
            env.root, songs_by_sha, songs_by_md5, material_rows,
            tables=working_tables, registry=load_difficulty_scale_registry(),
            target_folder_keys=maker_target_folders,
            target_keys=target_chart_keys(maker_target_charts),
        )
        analyses = {key: dict(value) for key, value in analyses.items()}
        for key, metric in maker_lookup.items():
            analyses.setdefault(key, {}).update(metric)

    views, missing_same = build_views(
        working_tables,
        [],
        [],
        generation_presets,
        songs_by_sha,
        songs_by_md5,
        scores,
        analyses=analyses,
        combinations=generated_combinations(settings),
    )
    total = 0
    density_cache: dict[str, dict[str, set[str]]] = {}
    for view in views:
        density_groups = density_cache.get(view.base_view_id)
        if density_groups is None:
            density_groups = density_rank_groups(view.charts, songs_by_sha, songs_by_md5)
            density_cache[view.base_view_id] = density_groups
        total += len(filter_charts(
            view.charts,
            view.preset,
            songs_by_sha,
            songs_by_md5,
            scores,
            analyses=analyses,
            density_groups_by_key=density_groups,
        ))
    return len(views), total, missing_same


def validate_default_json(path: Path) -> tuple[bool, str]:
    try:
        data = safe_load_json(path, [])
        if not isinstance(data, list):
            return False, "ルートが配列ではありません"
        for i, node in enumerate(data):
            if not isinstance(node, dict):
                return False, f"{i}番目がオブジェクトではありません"
            if not node.get("name"):
                return False, f"{i}番目にnameがありません"
        return True, f"{len(data)}個のルートフォルダ"
    except Exception as exc:
        return False, str(exc)


def summarize_environment(env: EnvironmentPaths) -> dict[str, str]:
    return {
        "environment": env.environment_label,
        "root": str(env.root),
        "config": "OK" if env.config.exists() else "なし",
        "song_db": "OK" if env.song_db.exists() else "なし",
        "table_dir": "OK" if env.table_dir.exists() else "なし",
        "default_json": "OK" if env.default_json.exists() else "新規作成",
        "score_db": str(env.score_db) if env.score_db else "未検出（プレイ系条件はNO PLAY扱い）",
        "player": env.player_name,
    }
