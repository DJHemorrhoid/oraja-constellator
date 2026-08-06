from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path
from typing import Any

FEATURE_SCHEMA_VERSION = 5
RULE_PACK_FORMAT = "bms_filter_attribute_rule_pack"
RULE_PACK_FORMAT_VERSION = 1

SUPPORTED_PRIMARY_RULE_IDS = {
    "ude_thick_repeat",
    "yubi_micro",
    "yubi_mid_band_ext",
    "yubi_low_speed_micro",
    "yubi_regular_chord",
    "delay_high_speed",
    "delay_low_speed_offgrid",
    "delay_mixed",
    "delay_tail",
    "delay_thin_high_speed",
    "ude_dense_low_speed_tail_randa",
    "ude_low_intensity_tail_randa",
    "randa_high_speed",
    "randa_middle_speed",
    "ude_low_speed_extended",
    "randa_thin_low_speed",
}


def _builtin_pack() -> dict[str, Any]:
    return {
        "format": RULE_PACK_FORMAT,
        "format_version": RULE_PACK_FORMAT_VERSION,
        "pack_id": "official_attribute_rules",
        "name": "標準譜面傾向分類ルール",
        "pack_version": "1.2.0",
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
        "description": "16分乱打・腕ガチ・指ガチ微縦連・ディレイの標準分類ルール。",
        "primary_rule_order": [
            "ude_thick_repeat",
            "yubi_micro",
            "yubi_mid_band_ext",
            "yubi_low_speed_micro",
            "yubi_regular_chord",
            "delay_high_speed",
            "delay_low_speed_offgrid",
            "delay_mixed",
            "delay_tail",
            "delay_thin_high_speed",
            "ude_dense_low_speed_tail_randa",
            "ude_low_intensity_tail_randa",
            "randa_high_speed",
            "randa_middle_speed",
            "ude_low_speed_extended",
            "randa_thin_low_speed",
        ],
        "rules": {
            "ude_thick_repeat": {
                "grid_max": 162.0, "chord_min": 2.85,
                "micro_chord_min": 0.26, "micro_full_min": 0.06,
                "full_alt_min": 0.30, "full_rate_min": 0.03,
            },
            "yubi_micro": {"micro_min": 0.075, "grid_min": 148.0, "grid_max": 205.0},
            "yubi_mid_band_ext": {
                "micro_min": 0.055, "grid_min": 148.0, "grid_max": 205.0,
                "subgrid_max_exclusive": 0.25, "repeat_sum_min": 0.04,
            },
            "yubi_low_speed_micro": {
                "micro_min": 0.065, "grid_min": 140.0, "grid_max_exclusive": 148.0,
                "micro_chord_max": 0.19,
            },
            "yubi_regular_chord": {
                "grid_min": 165.0, "grid_max_exclusive": 175.0,
                "chord_min": 2.4, "chord_ge3_min": 0.45,
                "subgrid_max_exclusive": 0.08, "micro_chord_max_exclusive": 0.12,
                "micro_min": 0.05, "repeat_sum_min": 0.04,
            },
            "delay_high_speed": {"grid_min": 228.0, "chord_max": 2.6},
            "delay_low_speed_offgrid": {"subgrid_min": 0.35, "chord_max": 2.05},
            "delay_mixed": {"subgrid_min": 0.22, "chord_max": 2.2, "grid_min": 150.0},
            "delay_tail": {
                "tail_sub_min": 0.26, "subgrid_min": 0.10,
                "chord_max": 2.15, "grid_min": 150.0,
            },
            "delay_thin_high_speed": {
                "grid_min": 205.0, "chord_max": 1.65, "subgrid_min": 0.08,
            },
            "ude_dense_low_speed_tail_randa": {
                "grid_max": 150.0, "chord_min": 2.5, "chord_ge3_min": 0.45,
                "tail_grid_min": 172.0, "tail_chord_max": 2.45,
            },
            "ude_low_intensity_tail_randa": {
                "grid_max": 140.0, "chord_min": 1.9, "chord_ge3_min": 0.22,
                "subgrid_max_exclusive": 0.25, "micro_max_exclusive": 0.06,
                "tail_grid_min": 172.0, "tail_chord_max": 2.45,
            },
            "randa_high_speed": {"grid_min": 175.0, "grid_max_exclusive": 228.0},
            "randa_middle_speed": {
                "grid_min": 146.0, "grid_max_exclusive": 175.0,
                "subgrid_max_exclusive": 0.20, "micro_max_exclusive": 0.075,
            },
            "ude_low_speed_extended": {
                "grid_max_exclusive": 150.0, "chord_min": 2.0,
                "chord_ge3_min": 0.30, "micro_max_exclusive": 0.06,
            },
            "randa_thin_low_speed": {
                "grid_min": 128.0, "grid_max_exclusive": 146.0,
                "chord_max_exclusive": 1.85, "subgrid_max_exclusive": 0.20,
                "micro_max_exclusive": 0.06,
            },
        },
        "scores": {
            "micro_valid_grid_min": 140.0,
            "yubi": {
                "micro_base": 0.035, "micro_width": 0.06,
                "grid_ceiling": 215.0, "grid_width": 15.0,
            },
            "delay": {
                "grid_base": 210.0, "grid_width": 22.0,
                "chord_ceiling_fast": 2.7, "chord_width_fast": 0.5,
                "subgrid_base": 0.10, "subgrid_width": 0.13,
                "chord_ceiling_mixed": 2.5, "chord_width_mixed": 0.6,
            },
            "ude": {
                "grid_ceiling": 162.0, "grid_width": 17.0,
                "chord_base": 2.0, "chord_width": 0.6,
                "chord_ge3_base": 0.25, "chord_ge3_width": 0.25,
            },
            "randa": {
                "grid_floor": 143.0, "grid_floor_width": 8.0,
                "grid_ceiling": 235.0, "grid_ceiling_width": 10.0,
                "micro_base": 0.04, "micro_width": 0.07,
                "subgrid_base": 0.18, "subgrid_width": 0.10,
            },
        },
        "fallback": {
            "score_threshold": 0.35,
            "low_grid_delay_guard_grid_max_exclusive": 145.0,
            "low_grid_delay_guard_subgrid_min": 0.35,
            "low_grid_delay_guard_chord_max": 2.05,
            "low_grid_ude_chord_min": 2.0,
            "low_grid_ude_chord_ge3_min": 0.30,
            "low_grid_ude_micro_max_exclusive": 0.06,
            "rule_primary_score_floor": 0.5,
        },
        "secondary": {
            "score_threshold": 0.35,
            "randa_yubi_micro_min": 0.02,
            "randa_yubi_grid_max": 210.0,
            "randa_delay_subgrid_min": 0.12,
            "delay_randa_grid_min": 175.0,
            "delay_randa_grid_max_exclusive": 228.0,
            "delay_randa_subgrid_max_exclusive": 0.35,
            "ude_yubi_grid_max_exclusive": 146.0,
            "ude_yubi_micro_min": 0.10,
            "ude_yubi_micro_chord_min": 0.15,
            "ude_randa_tail_grid_min": 165.0,
            "ude_randa_tail_chord_max": 2.6,
            "yubi_ude_chord_min": 2.7,
            "explicit_score_floor": 0.35,
        },
        "confidence": {
            "secondary_weight": 0.5, "minimum": 0.05, "maximum": 1.0,
        },
        "subcategory": {
            "randa_high_speed_grid_min": 195.0,
            "ude_full_rate_none_max_exclusive": 0.015,
            "ude_full_alt_min": 0.5,
            "yubi_long_jack_min": 0.008,
            "yubi_chord_min": 2.4,
            "yubi_micro_chord_ratio_min": 0.5,
        },
        "practice_low": {
            "duration_min": 60.0,
            "recovery_ratio_min": 0.40,
            "last_kill_min": 2.0,
            "burst_density_ratio_min": 2.3,
            "stream_max": 10.0,
        },
    }


def _deep_merge(base: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in incoming.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _canonical_hash(pack: dict[str, Any]) -> str:
    public = {k: v for k, v in pack.items() if not str(k).startswith("_")}
    payload = json.dumps(public, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def normalize_rule_pack(raw: dict[str, Any], source: Path | None = None) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("解析ルールパックのルートはオブジェクトである必要があります")
    merged = _deep_merge(_builtin_pack(), raw)
    if merged.get("format") != RULE_PACK_FORMAT:
        raise ValueError(f"解析ルールパック形式が不正です: {merged.get('format')!r}")
    if int(merged.get("format_version") or 0) != RULE_PACK_FORMAT_VERSION:
        raise ValueError("対応していない解析ルールパック形式です")
    pack_id = str(merged.get("pack_id") or "").strip()
    if not pack_id or not re.fullmatch(r"[A-Za-z0-9._-]+", pack_id):
        raise ValueError("解析ルールパックのpack_idには英数字・._-のみ使用できます")
    feature_version = int(merged.get("feature_schema_version") or 0)
    if feature_version != FEATURE_SCHEMA_VERSION:
        raise ValueError(
            f"特徴量形式v{feature_version}用のパックです。現在のツールはv{FEATURE_SCHEMA_VERSION}です"
        )
    order = merged.get("primary_rule_order")
    if not isinstance(order, list) or not order:
        raise ValueError("primary_rule_orderがありません")
    unknown = [str(rule_id) for rule_id in order if str(rule_id) not in SUPPORTED_PRIMARY_RULE_IDS]
    if unknown:
        raise ValueError("未対応の主分類ルールがあります: " + ", ".join(unknown))
    missing_rules = [str(rule_id) for rule_id in order if str(rule_id) not in (merged.get("rules") or {})]
    if missing_rules:
        raise ValueError("ルール定義がありません: " + ", ".join(missing_rules))
    merged["_source"] = str(source) if source else "builtin"
    merged["_hash"] = _canonical_hash(merged)
    return merged


def load_rule_pack_file(path: Path) -> dict[str, Any]:
    raw = json.loads(path.read_text(encoding="utf-8-sig"))
    return normalize_rule_pack(raw, path)


def load_rule_pack_directory(pack_dir: Path) -> tuple[dict[str, Any], list[str]]:
    paths = sorted(pack_dir.glob("*.json")) if pack_dir.exists() else []
    messages: list[str] = []
    packs: list[dict[str, Any]] = []
    for path in paths:
        try:
            packs.append(load_rule_pack_file(path))
        except Exception as exc:
            messages.append(f"{path.name}: {exc}")
    if messages:
        raise ValueError("\n".join(messages))
    if not packs:
        return normalize_rule_pack(_builtin_pack()), ["同梱の内蔵ルールを使用しています"]
    if len(packs) > 1:
        ids = ", ".join(str(pack.get("pack_id")) for pack in packs)
        raise ValueError(f"解析ルールパックは同時に1つだけ使用できます: {ids}")
    return packs[0], messages


_ACTIVE_RULE_PACK = normalize_rule_pack(_builtin_pack())


def get_active_rule_pack() -> dict[str, Any]:
    return _ACTIVE_RULE_PACK


def get_active_rule_pack_info() -> dict[str, str | int]:
    pack = _ACTIVE_RULE_PACK
    return {
        "pack_id": str(pack.get("pack_id") or ""),
        "name": str(pack.get("name") or pack.get("pack_id") or ""),
        "version": str(pack.get("pack_version") or ""),
        "feature_schema_version": int(pack.get("feature_schema_version") or 0),
        "hash": str(pack.get("_hash") or ""),
        "source": str(pack.get("_source") or ""),
    }


def set_active_rule_pack(pack: dict[str, Any]) -> dict[str, Any]:
    global _ACTIVE_RULE_PACK
    _ACTIVE_RULE_PACK = normalize_rule_pack(pack)
    return _ACTIVE_RULE_PACK


def reload_active_rule_pack(pack_dir: Path) -> tuple[dict[str, Any], list[str]]:
    global _ACTIVE_RULE_PACK
    pack, messages = load_rule_pack_directory(pack_dir)
    _ACTIVE_RULE_PACK = pack
    return pack, messages


def write_builtin_rule_pack(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(_builtin_pack(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
