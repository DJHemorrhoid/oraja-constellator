"""譜面属性解析モジュール (v0.9.1)

BMS譜面を実時間ベースで解析し、練習属性(16分乱打・腕ガチ・指ガチ微縦連・
ディレイ・中速厚め)を判定する。判定基準はプレイヤーのラベル付き教師データ
108譜面で検証済み(一致率92.6%)。詳細は docs/属性解析仕様.md を参照。

このモジュールは標準ライブラリのみに依存し、単体でも利用できる。
"""
from __future__ import annotations

import re
import sqlite3
import time
from bisect import bisect_right
from collections import defaultdict
from pathlib import Path
from typing import Any

from attribute_rule_pack import (
    get_active_rule_pack,
    get_active_rule_pack_info,
    reload_active_rule_pack,
)

ATTR_ANALYSIS_VERSION = 5  # 特徴量形式はv5のまま。分類ルール変更はルールハッシュで再分類する

# 属性コード: bmscf_chart_analysis.attr / attr2 に格納する整数値。
# 0 は「判定保留」(どの属性にも十分該当しない)を表す。
# 旧コード5「中速厚め」はv0.9.5で廃止し、16分乱打(副分類=中速乱打)へ移行する。
ATTR_CODE_UNDECIDED = 0
ATTR_CODE_LEGACY_MEDIUM = 5
ATTR_CODES: dict[str, int] = {
    "16分乱打": 1,
    "腕ガチ": 2,
    "指ガチ微縦連": 3,
    "ディレイ": 4,
}
ATTR_LABELS: dict[int, str] = {code: name for name, code in ATTR_CODES.items()}
ATTR_LABELS[ATTR_CODE_UNDECIDED] = "判定保留"
ATTR_VALUE_CHOICES: list[str] = [
    "16分乱打", "腕ガチ", "指ガチ微縦連", "ディレイ", "判定保留",
]

# 副分類コード (attr_sub)。0 は副分類なし。
SUBCAT_NONE = 0
SUBCAT_CODES: dict[str, int] = {
    "中速乱打": 1,
    "高速乱打": 2,
    "全押しなし": 11,
    "全押し交互": 12,
    "全押し多用": 13,
    "長縦連混じり": 21,
    "同時押し微縦連": 22,
}
SUBCAT_LABELS: dict[int, str] = {code: name for name, code in SUBCAT_CODES.items()}
SUBCAT_LABELS[SUBCAT_NONE] = "副分類なし"
SUBCAT_VALUE_CHOICES: list[str] = list(SUBCAT_CODES) + ["副分類なし"]
# 各主属性で有効な副分類 (フォルダ生成用)
SUBCATS_BY_ATTR: dict[int, list[int]] = {
    1: [SUBCAT_CODES["中速乱打"], SUBCAT_CODES["高速乱打"]],
    2: [SUBCAT_CODES["全押しなし"], SUBCAT_CODES["全押し交互"], SUBCAT_CODES["全押し多用"]],
    3: [SUBCAT_CODES["長縦連混じり"], SUBCAT_CODES["同時押し微縦連"]],
    4: [],
}

# 解析結果のうち chart_analysis / bmscf_chart_analysis へ保存する数値カラム。
ATTR_NUMERIC_COLUMNS: tuple[str, ...] = (
    "grid_bpm", "avg_chord", "chord_ge3", "micro_rate", "long_jack_rate",
    "subgrid_rate",
    "ongrid_rate", "stream_sec", "d2", "d10", "d20", "avg_density",
    "last_kill", "recovery_sec", "chart_seconds",
    "full_rate", "full_alt_rate", "micro_thin", "micro_chord", "micro_full",
    "tail_grid_bpm", "tail_chord", "tail_sub",
)
ATTR_INT_COLUMNS: tuple[str, ...] = ("attr", "attr2", "attr_sub", "practice_low", "total_notes", "is_dp")
ATTR_REAL_EXTRA: tuple[str, ...] = ("attr_conf",)


def migrate_legacy_attr(attr: Any, attr_sub: Any = None) -> tuple[Any, Any]:
    """旧コード5(中速厚め)を 16分乱打+中速乱打 へ変換する。"""
    if attr is not None and int(attr) == ATTR_CODE_LEGACY_MEDIUM:
        return ATTR_CODES["16分乱打"], SUBCAT_CODES["中速乱打"]
    return attr, attr_sub

_KEY_CH = {
    "11": 1, "12": 2, "13": 3, "14": 4, "15": 5, "18": 6, "19": 7, "16": 0,
    "21": 8, "22": 9, "23": 10, "24": 11, "25": 12, "28": 13, "29": 14, "26": 15,
}
_LN_CH = {
    "51": 1, "52": 2, "53": 3, "54": 4, "55": 5, "58": 6, "59": 7, "56": 0,
    "61": 8, "62": 9, "63": 10, "64": 11, "65": 12, "68": 13, "69": 14, "66": 15,
}

_DATA_RE = re.compile(r"^#(\d{3})([0-9A-Za-z]{2}):(.*)$")
_BPM_EXT_RE = re.compile(r"^#BPM([0-9A-Za-z]{2})\s+([\d.]+)", re.IGNORECASE)
_BPM_RE = re.compile(r"^#BPM\s+([\d.]+)", re.IGNORECASE)
_STOP_RE = re.compile(r"^#STOP([0-9A-Za-z]{2})\s+([\d.]+)", re.IGNORECASE)
_LNOBJ_RE = re.compile(r"^#LNOBJ\s+([0-9A-Za-z]{2})", re.IGNORECASE)


def _read_text(path: Path) -> str:
    raw = path.read_bytes()
    for encoding in ("shift_jis", "cp932", "utf-8"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("shift_jis", errors="replace")


def parse_note_times(path: Path) -> tuple[list[tuple[float, int]], bool]:
    """全可視ノーツの (実時間秒, レーン) を返す。BPM変化・STOP・小節長対応。

    LNは始点のみ1ノーツとして数える。#RANDOM は第1分岐のみ採用。
    戻り値の第2要素は2P側チャンネルの使用有無 (DP譜面フラグ)。
    """
    text = _read_text(path)
    base_bpm = 130.0
    bpm_defs: dict[str, float] = {}
    stop_defs: dict[str, float] = {}
    lnobj = ""
    measure_lengths: dict[int, float] = {}
    measures: dict[int, list[tuple[str, str]]] = defaultdict(list)
    # BMSの制御構文は「各RANDOMで1が選ばれた」とみなし、#IF 1側だけを採用する。
    # #ELSE / #ELSEIF とネストを処理しないと、複数分岐が合成された架空譜面になる。
    branch_stack: list[dict[str, bool]] = []

    def current_active() -> bool:
        return all(frame["active"] for frame in branch_stack)

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line.startswith("#"):
            continue
        upper = line.upper()
        if upper.startswith("#RANDOM") or upper.startswith("#SETRANDOM"):
            continue
        if upper.startswith("#ELSEIF"):
            if branch_stack:
                frame = branch_stack[-1]
                try:
                    branch = int(upper.split()[1])
                except (IndexError, ValueError):
                    branch = 1
                take = frame["parent_active"] and not frame["matched"] and branch == 1
                frame["active"] = take
                if take:
                    frame["matched"] = True
            continue
        if upper.startswith("#IF"):
            try:
                branch = int(upper.split()[1])
            except (IndexError, ValueError):
                branch = 1
            parent_active = current_active()
            take = parent_active and branch == 1
            branch_stack.append({
                "parent_active": parent_active,
                "matched": take,
                "active": take,
            })
            continue
        if upper.startswith("#ELSE"):
            if branch_stack:
                frame = branch_stack[-1]
                take = frame["parent_active"] and not frame["matched"]
                frame["active"] = take
                frame["matched"] = True
            continue
        if upper.startswith("#ENDIF"):
            if branch_stack:
                branch_stack.pop()
            continue
        if not current_active():
            continue
        m = _DATA_RE.match(line)
        if m:
            measure = int(m.group(1))
            channel = m.group(2).upper()
            value = m.group(3).strip()
            if channel == "02":
                try:
                    measure_lengths[measure] = float(value)
                except ValueError:
                    pass
            else:
                measures[measure].append((channel, value))
            continue
        m = _BPM_EXT_RE.match(line)
        if m:
            try:
                bpm_defs[m.group(1).upper()] = float(m.group(2))
            except ValueError:
                pass
            continue
        m = _BPM_RE.match(line)
        if m:
            try:
                base_bpm = float(m.group(1))
            except ValueError:
                pass
            continue
        m = _STOP_RE.match(line)
        if m:
            try:
                stop_defs[m.group(1).upper()] = float(m.group(2))
            except ValueError:
                pass
            continue
        m = _LNOBJ_RE.match(line)
        if m:
            lnobj = m.group(1).upper()
    if not measures:
        return [], False

    events: list[tuple[int, float, int, float]] = []  # (measure, pos, kind, value)
    KIND_BPM, KIND_STOP, KIND_NOTE = 0, 1, 2
    ln_seen: dict[int, int] = defaultdict(int)
    for measure in sorted(measures):
        row_events: list[tuple[float, int, float]] = []
        for channel, value in measures[measure]:
            count = len(value) // 2
            if count <= 0:
                continue
            for i in range(count):
                token = value[i * 2:i * 2 + 2].upper()
                if token == "00":
                    continue
                pos = i / count
                if channel == "03":
                    try:
                        row_events.append((pos, KIND_BPM, float(int(token, 16))))
                    except ValueError:
                        pass
                elif channel == "08":
                    bpm = bpm_defs.get(token)
                    if bpm and bpm > 0:
                        row_events.append((pos, KIND_BPM, bpm))
                elif channel == "09":
                    stop = stop_defs.get(token)
                    if stop:
                        row_events.append((pos, KIND_STOP, stop))
                elif channel in _KEY_CH:
                    if lnobj and token == lnobj:
                        continue
                    row_events.append((pos, KIND_NOTE, float(_KEY_CH[channel])))
                elif channel in _LN_CH:
                    lane = _LN_CH[channel]
                    ln_seen[lane] += 1
                    if ln_seen[lane] % 2 == 1:
                        row_events.append((pos, KIND_NOTE, float(lane)))
        # 同一位置ではBPM変化→STOP→ノーツの順に適用する
        row_events.sort(key=lambda item: (item[0], item[1]))
        for pos, kind, value in row_events:
            events.append((measure, pos, kind, value))

    max_measure = max(measures)
    notes: list[tuple[float, int]] = []
    now = 0.0
    bpm = base_bpm
    index = 0
    total_events = len(events)
    for measure in range(max_measure + 1):
        beats = 4.0 * measure_lengths.get(measure, 1.0)
        prev_pos = 0.0
        while index < total_events and events[index][0] == measure:
            _, pos, kind, value = events[index]
            index += 1
            now += (pos - prev_pos) * beats * 60.0 / bpm
            prev_pos = pos
            if kind == 0:
                if value > 0:
                    bpm = value
            elif kind == 1:
                now += value / 192.0 * 4.0 * 60.0 / bpm
            else:
                notes.append((now, int(value)))
        now += (1.0 - prev_pos) * beats * 60.0 / bpm
    notes.sort(key=lambda item: item[0])
    is_dp = any(lane >= 8 for _t, lane in notes)
    return notes, is_dp


def _clip(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def extract_attr_features(path: Path) -> dict[str, Any]:
    """譜面から属性判定用の特徴量を抽出する。ノーツ不足時は ValueError。"""
    notes, is_dp = parse_note_times(path)
    total = len(notes)
    if total < 50:
        raise ValueError(f"ノーツ数が不足しています ({total})")
    duration = notes[-1][0] - notes[0][0]
    if duration <= 10.0:
        raise ValueError(f"演奏時間が短すぎます ({duration:.1f}s)")

    rows: list[tuple[float, list[int]]] = []
    for note_time, lane in notes:
        if rows and note_time - rows[-1][0] <= 0.0015:
            rows[-1][1].append(lane)
        else:
            rows.append((note_time, [lane]))
    row_times = [row[0] for row in rows]
    row_sizes = [len(row[1]) for row in rows]
    intervals = [row_times[i + 1] - row_times[i] for i in range(len(row_times) - 1)]
    if not intervals:
        raise ValueError("行間隔を計算できません")

    # グリッド推定: 30〜250msの行間隔の最頻値(2msビン)。
    # 低難度の疎な譜面では休符を挟んだ8分・付点間隔が最頻になり、本来の
    # 16分がグリッド外(ズレ)として誤検出される。そこで最頻値の半分の間隔に
    # 有意な質量(40%以上)がある場合は、その半分を真のグリッドとみなす調波補正を
    # 行う。厚い同時押し主体(平均2.1以上)の譜面は8分ガチ押し+16分装飾である
    # 可能性が高いため補正しない。
    provisional_chord = total / len(rows)
    histogram: dict[int, int] = defaultdict(int)
    for interval in intervals:
        if 0.030 <= interval <= 0.250:
            histogram[int(round(interval * 500))] += 1
    if histogram:
        mode_bin = max(histogram, key=histogram.get)
        mode_count = histogram[mode_bin]
        half_bin = mode_bin / 2.0
        half_mass = sum(
            histogram.get(b, 0)
            for b in (int(half_bin) - 1, int(half_bin), int(half_bin) + 1)
        )
        refined_bpm = 60.0 / ((half_bin / 500.0) * 4.0) if half_bin > 0 else 9999.0
        if (
            half_bin >= 15
            and half_mass >= 0.40 * mode_count
            and provisional_chord < 2.1
            and refined_bpm <= 252.0  # 32分ズレを16分グリッドと誤認しない
        ):
            grid = half_bin / 500.0
        else:
            grid = mode_bin / 500.0
    else:
        grid = 0.1
    grid_bpm = 60.0 / (grid * 4.0)

    note_times = [note_time for note_time, _lane in notes]

    def peak_density(window: float) -> float:
        best = 0.0
        left = 0
        for right in range(total):
            while note_times[right] - note_times[left] > window:
                left += 1
            best = max(best, (right - left + 1) / window)
        return best

    d2 = peak_density(2.0)
    d10 = peak_density(10.0)
    d20 = peak_density(20.0)
    avg_density = total / duration

    row_count = len(rows)
    avg_chord = total / row_count
    chord_ge3 = sum(1 for size in row_sizes if size >= 3) / row_count

    sub_count = sum(1 for iv in intervals if 0.004 < iv < 0.7 * grid)
    subgrid_rate = sub_count / len(intervals)
    ongrid_rate = sum(1 for iv in intervals if 0.8 * grid <= iv <= 1.25 * grid) / len(intervals)

    # 微縦連: 同一レーンの2〜4連打 (間隔 <= min(1.3*grid, 115ms) かつ <= 105ms)
    jack_gate = min(1.3 * grid, 0.115)
    lane_times: dict[int, list[float]] = defaultdict(list)
    for note_time, lane in notes:
        lane_times[lane].append(note_time)
    runs: list[int] = []
    for times in lane_times.values():
        run = 1
        for i in range(1, len(times)):
            if times[i] - times[i - 1] <= jack_gate and times[i] - times[i - 1] <= 0.105:
                run += 1
            else:
                if run >= 2:
                    runs.append(run)
                run = 1
        if run >= 2:
            runs.append(run)
    micro_rate = sum(run for run in runs if 2 <= run <= 4) / total
    long_jack_rate = sum(run for run in runs if run >= 5) / total

    # 最長発狂: 行間隔 <= 1.3*grid+5ms が0.3秒超の切れ目なしに続く最長区間
    stream_sec = 0.0
    stream_start: float | None = None
    stream_last = 0.0
    for i, interval in enumerate(intervals):
        if interval <= 1.3 * grid + 0.005:
            if stream_start is None:
                stream_start = row_times[i]
            stream_last = row_times[i + 1]
        elif interval > 0.3:
            if stream_start is not None:
                stream_sec = max(stream_sec, stream_last - stream_start)
                stream_start = None
    if stream_start is not None:
        stream_sec = max(stream_sec, stream_last - stream_start)

    # ラス殺し度: 終盤10%区間の密度 ÷ 全体平均密度
    tail_start = notes[0][0] + duration * 0.9
    tail_notes = total - bisect_right(note_times, tail_start)
    tail_density = tail_notes / max(duration * 0.1, 1e-9)
    last_kill = tail_density / max(avg_density, 1e-9)

    # 回復地帯: 2秒ビンの密度が全体平均の40%未満である合計秒数(端ビン除く)
    bin_counts: dict[int, int] = defaultdict(int)
    origin = notes[0][0]
    for note_time in note_times:
        bin_counts[int((note_time - origin) / 2.0)] += 1
    last_bin = int(duration / 2.0)
    recovery_sec = 0.0
    for bin_index in range(1, last_bin):
        if bin_counts.get(bin_index, 0) / 2.0 < avg_density * 0.4:
            recovery_sec += 2.0

    # --- v0.9.5 追加特徴量 ---
    # 鍵盤(1P/2P鍵盤レーン)のみの行サイズ。全押し検出は皿を含めない。
    key_sizes = [sum(1 for lane in row[1] if lane % 8 != 0) for row in rows]

    # 全押し構造: 6鍵以上の行の割合と、その隣接行が2〜5鍵である交互性
    full_indices = [i for i, size in enumerate(key_sizes) if size >= 6]
    full_rate = len(full_indices) / row_count
    alternate_hits = 0
    for i in full_indices:
        near: list[int] = []
        if i > 0 and row_times[i] - row_times[i - 1] <= 1.5 * grid + 0.01:
            near.append(key_sizes[i - 1])
        if i + 1 < row_count and row_times[i + 1] - row_times[i] <= 1.5 * grid + 0.01:
            near.append(key_sizes[i + 1])
        if near and all(2 <= size <= 5 for size in near):
            alternate_hits += 1
    full_alt_rate = alternate_hits / len(full_indices) if full_indices else 0.0

    # 微縦連の行厚み分解: 同一レーンrunが跨ぐ行の最大鍵盤サイズで分類する。
    # thin(<=2鍵) は単鍵微縦連、chord(3-5鍵) は同時押し埋め込み、full(>=6鍵) は全押し反復由来。
    # ノーツ時刻→行番号は二分探索で引く。1.5ms行マージにより行開始時刻と
    # ノーツ時刻がずれること、同一レーンの重複ノーツがあることに対して安全。
    micro_thin = micro_chord = micro_full = 0

    def _row_index_of(note_time: float) -> int:
        return max(0, bisect_right(row_times, note_time + 1e-9) - 1)

    def _classify_run(lane: int, run: list[float]) -> None:
        nonlocal micro_thin, micro_chord, micro_full
        if len(run) < 2 or len(run) > 4:
            return
        max_size = max(key_sizes[_row_index_of(t)] for t in run)
        if max_size >= 6:
            micro_full += len(run)
        elif max_size >= 3:
            micro_chord += len(run)
        else:
            micro_thin += len(run)

    for lane, times in lane_times.items():
        run = [times[0]]
        for i in range(1, len(times)):
            delta = times[i] - times[i - 1]
            if delta <= jack_gate and delta <= 0.105:
                run.append(times[i])
            else:
                _classify_run(lane, run)
                run = [times[i]]
        _classify_run(lane, run)
    micro_thin_rate = micro_thin / total
    micro_chord_rate = micro_chord / total
    micro_full_rate = micro_full / total

    # 終盤区間 (末尾 max(20秒, 全体の25%)): イージーゲージ観点でラストの傾向を別枠計測
    tail_span = max(20.0, duration * 0.25)
    tail_from = notes[-1][0] - tail_span
    tail_first = next((i for i, row in enumerate(rows) if row[0] >= tail_from), row_count - 1)
    tail_rows = rows[tail_first:]
    tail_intervals = [tail_rows[i + 1][0] - tail_rows[i][0] for i in range(len(tail_rows) - 1)]
    if len(tail_intervals) >= 20:
        tail_histogram: dict[int, int] = defaultdict(int)
        for interval in tail_intervals:
            if 0.030 <= interval <= 0.250:
                tail_histogram[int(round(interval * 500))] += 1
        if tail_histogram:
            tail_mode = max(tail_histogram, key=tail_histogram.get)
            tail_half = tail_mode / 2.0
            tail_half_mass = sum(
                tail_histogram.get(b, 0)
                for b in (int(tail_half) - 1, int(tail_half), int(tail_half) + 1)
            )
            tail_note_total = sum(len(row[1]) for row in tail_rows)
            tail_chord_prov = tail_note_total / len(tail_rows)
            tail_refined_bpm = 60.0 / ((tail_half / 500.0) * 4.0) if tail_half > 0 else 9999.0
            if (
                tail_half >= 15
                and tail_half_mass >= 0.40 * tail_histogram[tail_mode]
                and tail_chord_prov < 2.1
                and tail_refined_bpm <= 252.0
            ):
                tail_grid = tail_half / 500.0
            else:
                tail_grid = tail_mode / 500.0
        else:
            tail_grid = grid
        tail_grid_bpm = 60.0 / (tail_grid * 4.0)
        tail_note_count = sum(len(row[1]) for row in tail_rows)
        tail_chord = tail_note_count / len(tail_rows)
        tail_sub = sum(1 for iv in tail_intervals if 0.004 < iv < 0.7 * tail_grid) / len(tail_intervals)
    else:
        tail_grid_bpm = grid_bpm
        tail_chord = avg_chord
        tail_sub = subgrid_rate

    return {
        "grid_bpm": round(grid_bpm, 1),
        "avg_chord": round(avg_chord, 3),
        "chord_ge3": round(chord_ge3, 4),
        "micro_rate": round(micro_rate, 4),
        "long_jack_rate": round(long_jack_rate, 4),
        "subgrid_rate": round(subgrid_rate, 4),
        "ongrid_rate": round(ongrid_rate, 4),
        "stream_sec": round(stream_sec, 1),
        "d2": round(d2, 2),
        "d10": round(d10, 2),
        "d20": round(d20, 2),
        "avg_density": round(avg_density, 2),
        "last_kill": round(last_kill, 3),
        "recovery_sec": round(recovery_sec, 1),
        "total_notes": total,
        "chart_seconds": round(duration, 1),
        "is_dp": 1 if is_dp else 0,
        "full_rate": round(full_rate, 4),
        "full_alt_rate": round(full_alt_rate, 3),
        "micro_thin": round(micro_thin_rate, 4),
        "micro_chord": round(micro_chord_rate, 4),
        "micro_full": round(micro_full_rate, 4),
        "tail_grid_bpm": round(tail_grid_bpm, 1),
        "tail_chord": round(tail_chord, 3),
        "tail_sub": round(tail_sub, 4),
    }


def _rule_section(name: str) -> dict[str, Any]:
    value = get_active_rule_pack().get(name)
    return value if isinstance(value, dict) else {}


def _attr_scores(f: dict[str, Any]) -> dict[int, float]:
    """各属性の該当度スコア(0〜1)。係数は解析ルールパックから取得する。"""
    cfg = _rule_section("scores")
    grid = float(f["grid_bpm"])
    chord = float(f["avg_chord"])
    ge3 = float(f["chord_ge3"])
    micro = float(f["micro_rate"])
    sub = float(f["subgrid_rate"])
    micro_valid = micro if grid >= float(cfg.get("micro_valid_grid_min", 140.0)) else 0.0

    yubi = cfg.get("yubi") or {}
    delay = cfg.get("delay") or {}
    ude = cfg.get("ude") or {}
    randa = cfg.get("randa") or {}
    scores: dict[int, float] = {}
    scores[ATTR_CODES["指ガチ微縦連"]] = (
        _clip((micro_valid - float(yubi.get("micro_base", 0.035))) / float(yubi.get("micro_width", 0.06)))
        * _clip((float(yubi.get("grid_ceiling", 215.0)) - grid) / float(yubi.get("grid_width", 15.0)))
    )
    scores[ATTR_CODES["ディレイ"]] = max(
        _clip((grid - float(delay.get("grid_base", 210.0))) / float(delay.get("grid_width", 22.0)))
        * _clip((float(delay.get("chord_ceiling_fast", 2.7)) - chord) / float(delay.get("chord_width_fast", 0.5))),
        _clip((sub - float(delay.get("subgrid_base", 0.10))) / float(delay.get("subgrid_width", 0.13)))
        * _clip((float(delay.get("chord_ceiling_mixed", 2.5)) - chord) / float(delay.get("chord_width_mixed", 0.6))),
    )
    scores[ATTR_CODES["腕ガチ"]] = (
        _clip((float(ude.get("grid_ceiling", 162.0)) - grid) / float(ude.get("grid_width", 17.0)))
        * max(
            _clip((chord - float(ude.get("chord_base", 2.0))) / float(ude.get("chord_width", 0.6))),
            _clip((ge3 - float(ude.get("chord_ge3_base", 0.25))) / float(ude.get("chord_ge3_width", 0.25))),
        )
    )
    scores[ATTR_CODES["16分乱打"]] = (
        _clip((grid - float(randa.get("grid_floor", 143.0))) / float(randa.get("grid_floor_width", 8.0)))
        * _clip((float(randa.get("grid_ceiling", 235.0)) - grid) / float(randa.get("grid_ceiling_width", 10.0)))
        * (1.0 - _clip((micro_valid - float(randa.get("micro_base", 0.04))) / float(randa.get("micro_width", 0.07))))
        * (1.0 - _clip((sub - float(randa.get("subgrid_base", 0.18))) / float(randa.get("subgrid_width", 0.10))))
    )
    return scores


def _matches_primary_rule(rule_id: str, f: dict[str, Any]) -> int:
    rules = _rule_section("rules")
    c = rules.get(rule_id) or {}
    grid = float(f["grid_bpm"])
    chord = float(f["avg_chord"])
    ge3 = float(f["chord_ge3"])
    micro = float(f["micro_rate"])
    sub = float(f["subgrid_rate"])
    m_chord = float(f.get("micro_chord") or 0.0)
    m_full = float(f.get("micro_full") or 0.0)
    full_alt = float(f.get("full_alt_rate") or 0.0)
    full_rate = float(f.get("full_rate") or 0.0)
    tail_grid = float(f.get("tail_grid_bpm") or grid)
    tail_chord = float(f.get("tail_chord") or chord)
    tail_sub = float(f.get("tail_sub") or sub)

    if rule_id == "ude_thick_repeat":
        ok = grid <= float(c["grid_max"]) and chord >= float(c["chord_min"]) and (
            m_chord >= float(c["micro_chord_min"])
            or m_full >= float(c["micro_full_min"])
            or (full_alt >= float(c["full_alt_min"]) and full_rate >= float(c["full_rate_min"]))
        )
        return ATTR_CODES["腕ガチ"] if ok else ATTR_CODE_UNDECIDED
    if rule_id == "yubi_micro":
        ok = micro >= float(c["micro_min"]) and float(c["grid_min"]) <= grid <= float(c["grid_max"])
        return ATTR_CODES["指ガチ微縦連"] if ok else ATTR_CODE_UNDECIDED
    if rule_id == "yubi_mid_band_ext":
        repeat_sum = float(f.get("micro_thin") or 0.0) + m_chord + m_full
        ok = (
            micro >= float(c["micro_min"])
            and float(c["grid_min"]) <= grid <= float(c["grid_max"])
            and sub < float(c["subgrid_max_exclusive"])
            and repeat_sum >= float(c["repeat_sum_min"])
        )
        return ATTR_CODES["指ガチ微縦連"] if ok else ATTR_CODE_UNDECIDED
    if rule_id == "yubi_low_speed_micro":
        ok = (
            micro >= float(c["micro_min"])
            and float(c["grid_min"]) <= grid < float(c["grid_max_exclusive"])
            and m_chord <= float(c["micro_chord_max"])
        )
        return ATTR_CODES["指ガチ微縦連"] if ok else ATTR_CODE_UNDECIDED
    if rule_id == "yubi_regular_chord":
        repeat_sum = m_chord + m_full
        ok = (
            float(c["grid_min"]) <= grid < float(c["grid_max_exclusive"])
            and chord >= float(c["chord_min"])
            and ge3 >= float(c["chord_ge3_min"])
            and sub < float(c["subgrid_max_exclusive"])
            and m_chord < float(c["micro_chord_max_exclusive"])
            and micro >= float(c.get("micro_min", 0.0))
            and repeat_sum >= float(c.get("repeat_sum_min", 0.0))
        )
        return ATTR_CODES["指ガチ微縦連"] if ok else ATTR_CODE_UNDECIDED
    if rule_id == "delay_high_speed":
        ok = grid >= float(c["grid_min"]) and chord <= float(c["chord_max"])
        return ATTR_CODES["ディレイ"] if ok else ATTR_CODE_UNDECIDED
    if rule_id == "delay_low_speed_offgrid":
        ok = sub >= float(c["subgrid_min"]) and chord <= float(c["chord_max"])
        return ATTR_CODES["ディレイ"] if ok else ATTR_CODE_UNDECIDED
    if rule_id == "delay_mixed":
        ok = sub >= float(c["subgrid_min"]) and chord <= float(c["chord_max"]) and grid >= float(c["grid_min"])
        return ATTR_CODES["ディレイ"] if ok else ATTR_CODE_UNDECIDED
    if rule_id == "delay_tail":
        ok = (
            tail_sub >= float(c["tail_sub_min"])
            and sub >= float(c["subgrid_min"])
            and chord <= float(c["chord_max"])
            and grid >= float(c["grid_min"])
        )
        return ATTR_CODES["ディレイ"] if ok else ATTR_CODE_UNDECIDED
    if rule_id == "delay_thin_high_speed":
        ok = grid >= float(c["grid_min"]) and chord <= float(c["chord_max"]) and sub >= float(c["subgrid_min"])
        return ATTR_CODES["ディレイ"] if ok else ATTR_CODE_UNDECIDED
    if rule_id in {"ude_dense_low_speed_tail_randa", "ude_low_intensity_tail_randa"}:
        if rule_id == "ude_dense_low_speed_tail_randa":
            ok = grid <= float(c["grid_max"]) and (chord >= float(c["chord_min"]) or ge3 >= float(c["chord_ge3_min"]))
        else:
            ok = (
                grid <= float(c["grid_max"])
                and chord >= float(c["chord_min"])
                and ge3 >= float(c["chord_ge3_min"])
                and sub < float(c["subgrid_max_exclusive"])
                and micro < float(c["micro_max_exclusive"])
            )
        if not ok:
            return ATTR_CODE_UNDECIDED
        tail_is_randa = tail_grid >= float(c["tail_grid_min"]) and tail_chord <= float(c["tail_chord_max"])
        return ATTR_CODES["16分乱打"] if tail_is_randa else ATTR_CODES["腕ガチ"]
    if rule_id == "randa_high_speed":
        ok = float(c["grid_min"]) <= grid < float(c["grid_max_exclusive"])
        return ATTR_CODES["16分乱打"] if ok else ATTR_CODE_UNDECIDED
    if rule_id == "randa_middle_speed":
        ok = (
            float(c["grid_min"]) <= grid < float(c["grid_max_exclusive"])
            and sub < float(c["subgrid_max_exclusive"])
            and micro < float(c["micro_max_exclusive"])
        )
        return ATTR_CODES["16分乱打"] if ok else ATTR_CODE_UNDECIDED
    if rule_id == "ude_low_speed_extended":
        ok = (
            grid < float(c["grid_max_exclusive"])
            and chord >= float(c["chord_min"])
            and ge3 >= float(c["chord_ge3_min"])
            and micro < float(c["micro_max_exclusive"])
        )
        return ATTR_CODES["腕ガチ"] if ok else ATTR_CODE_UNDECIDED
    if rule_id == "randa_thin_low_speed":
        ok = (
            float(c["grid_min"]) <= grid < float(c["grid_max_exclusive"])
            and chord < float(c["chord_max_exclusive"])
            and sub < float(c["subgrid_max_exclusive"])
            and micro < float(c["micro_max_exclusive"])
        )
        return ATTR_CODES["16分乱打"] if ok else ATTR_CODE_UNDECIDED
    return ATTR_CODE_UNDECIDED


def classify_attr(f: dict[str, Any]) -> tuple[int, int, float]:
    """(第一属性, 第二属性, 判定信頼度)を解析ルールパックに従って返す。"""
    pack = get_active_rule_pack()
    grid = float(f["grid_bpm"])
    chord = float(f["avg_chord"])
    ge3 = float(f["chord_ge3"])
    micro = float(f["micro_rate"])
    sub = float(f["subgrid_rate"])
    m_chord = float(f.get("micro_chord") or 0.0)
    tail_grid = float(f.get("tail_grid_bpm") or grid)
    tail_chord = float(f.get("tail_chord") or chord)

    primary = ATTR_CODE_UNDECIDED
    for raw_rule_id in pack.get("primary_rule_order") or []:
        primary = _matches_primary_rule(str(raw_rule_id), f)
        if primary != ATTR_CODE_UNDECIDED:
            break

    scores = _attr_scores(f)
    fallback = _rule_section("fallback")
    if primary == ATTR_CODE_UNDECIDED:
        best_code = max(scores, key=lambda code: scores[code])
        if scores[best_code] >= float(fallback.get("score_threshold", 0.35)):
            primary = best_code
        if (
            primary == ATTR_CODES["ディレイ"]
            and grid < float(fallback.get("low_grid_delay_guard_grid_max_exclusive", 145.0))
            and not (
                sub >= float(fallback.get("low_grid_delay_guard_subgrid_min", 0.35))
                and chord <= float(fallback.get("low_grid_delay_guard_chord_max", 2.05))
            )
        ):
            if (
                chord >= float(fallback.get("low_grid_ude_chord_min", 2.0))
                and ge3 >= float(fallback.get("low_grid_ude_chord_ge3_min", 0.30))
                and micro < float(fallback.get("low_grid_ude_micro_max_exclusive", 0.06))
            ):
                primary = ATTR_CODES["腕ガチ"]
            else:
                primary = ATTR_CODE_UNDECIDED
    primary_score = scores.get(primary, 0.0) if primary else 0.0
    if primary and primary_score < float(fallback.get("rule_primary_score_floor", 0.5)):
        primary_score = float(fallback.get("rule_primary_score_floor", 0.5))

    secondary_cfg = _rule_section("secondary")
    secondary = ATTR_CODE_UNDECIDED
    secondary_score = 0.0
    for code, score in sorted(scores.items(), key=lambda item: -item[1]):
        if code != primary and score >= float(secondary_cfg.get("score_threshold", 0.35)):
            secondary = code
            secondary_score = score
            break
    if secondary == ATTR_CODE_UNDECIDED and primary != ATTR_CODE_UNDECIDED:
        randa = ATTR_CODES["16分乱打"]
        yubi = ATTR_CODES["指ガチ微縦連"]
        delay = ATTR_CODES["ディレイ"]
        ude = ATTR_CODES["腕ガチ"]
        if (
            primary == randa
            and micro >= float(secondary_cfg.get("randa_yubi_micro_min", 0.02))
            and grid <= float(secondary_cfg.get("randa_yubi_grid_max", 210.0))
        ):
            secondary = yubi
        elif primary == randa and sub >= float(secondary_cfg.get("randa_delay_subgrid_min", 0.12)):
            secondary = delay
        elif (
            primary == delay
            and float(secondary_cfg.get("delay_randa_grid_min", 175.0)) <= grid
            < float(secondary_cfg.get("delay_randa_grid_max_exclusive", 228.0))
            and sub < float(secondary_cfg.get("delay_randa_subgrid_max_exclusive", 0.35))
        ):
            secondary = randa
        elif (
            primary == ude
            and grid < float(secondary_cfg.get("ude_yubi_grid_max_exclusive", 146.0))
            and (
                micro >= float(secondary_cfg.get("ude_yubi_micro_min", 0.10))
                or m_chord >= float(secondary_cfg.get("ude_yubi_micro_chord_min", 0.15))
            )
        ):
            secondary = yubi
        elif (
            primary == ude
            and tail_grid >= float(secondary_cfg.get("ude_randa_tail_grid_min", 165.0))
            and tail_chord <= float(secondary_cfg.get("ude_randa_tail_chord_max", 2.6))
        ):
            secondary = randa
        elif primary == yubi and chord >= float(secondary_cfg.get("yubi_ude_chord_min", 2.7)):
            secondary = ude
        if secondary != ATTR_CODE_UNDECIDED:
            secondary_score = max(
                secondary_score,
                scores.get(secondary, 0.0),
                float(secondary_cfg.get("explicit_score_floor", 0.35)),
            )

    confidence_cfg = _rule_section("confidence")
    if primary == ATTR_CODE_UNDECIDED:
        confidence = 0.0
    else:
        confidence = round(
            _clip(
                primary_score - secondary_score * float(confidence_cfg.get("secondary_weight", 0.5)),
                float(confidence_cfg.get("minimum", 0.05)),
                float(confidence_cfg.get("maximum", 1.0)),
            ),
            3,
        )
    return primary, secondary, confidence


def classify_subcategory(attr: int, f: dict[str, Any]) -> int:
    """主属性に応じた副分類コードを解析ルールパックに従って返す。"""
    cfg = _rule_section("subcategory")
    grid = float(f["grid_bpm"])
    chord = float(f["avg_chord"])
    micro = float(f["micro_rate"])
    m_chord = float(f.get("micro_chord") or 0.0)
    full_rate = float(f.get("full_rate") or 0.0)
    full_alt = float(f.get("full_alt_rate") or 0.0)
    long_jack = float(f.get("long_jack_rate") or 0.0)
    if attr == ATTR_CODES["16分乱打"]:
        return (
            SUBCAT_CODES["高速乱打"]
            if grid >= float(cfg.get("randa_high_speed_grid_min", 195.0))
            else SUBCAT_CODES["中速乱打"]
        )
    if attr == ATTR_CODES["腕ガチ"]:
        if full_rate < float(cfg.get("ude_full_rate_none_max_exclusive", 0.015)):
            return SUBCAT_CODES["全押しなし"]
        if full_alt >= float(cfg.get("ude_full_alt_min", 0.5)):
            return SUBCAT_CODES["全押し交互"]
        return SUBCAT_CODES["全押し多用"]
    if attr == ATTR_CODES["指ガチ微縦連"]:
        if long_jack >= float(cfg.get("yubi_long_jack_min", 0.008)):
            return SUBCAT_CODES["長縦連混じり"]
        if chord >= float(cfg.get("yubi_chord_min", 2.4)) or (
            micro > 0 and m_chord >= float(cfg.get("yubi_micro_chord_ratio_min", 0.5)) * micro
        ):
            return SUBCAT_CODES["同時押し微縦連"]
        return SUBCAT_NONE
    return SUBCAT_NONE


def classify_practice_priority(f: dict[str, Any]) -> int:
    """反復練習素材としての効率が低い譜面なら1を返す。"""
    cfg = _rule_section("practice_low")
    duration = float(f.get("chart_seconds") or 0.0)
    recovery = float(f.get("recovery_sec") or 0.0)
    last_kill = float(f.get("last_kill") or 0.0)
    d10 = float(f.get("d10") or 0.0)
    avg_density = float(f.get("avg_density") or 0.0)
    stream = float(f.get("stream_sec") or 0.0)
    duration_min = float(cfg.get("duration_min", 60.0))
    if duration >= duration_min and recovery / max(duration, 1.0) >= float(cfg.get("recovery_ratio_min", 0.40)):
        return 1
    if last_kill >= float(cfg.get("last_kill_min", 2.0)):
        return 1
    if avg_density > 0 and d10 / avg_density >= float(cfg.get("burst_density_ratio_min", 2.3)):
        return 1
    if stream <= float(cfg.get("stream_max", 10.0)) and duration >= duration_min:
        return 1
    return 0

def analyze_attr_file(path: Path) -> dict[str, Any]:
    """属性解析の入口。特徴量+判定結果の辞書を返す。"""
    features = extract_attr_features(path)
    attr, attr2, conf = classify_attr(features)
    features["attr"] = attr
    features["attr2"] = attr2
    features["attr_conf"] = conf
    features["attr_sub"] = classify_subcategory(attr, features)
    features["practice_low"] = classify_practice_priority(features)
    return features


def ensure_override_table(con: sqlite3.Connection) -> None:
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS chart_attr_override (
            sha256 TEXT PRIMARY KEY,
            attr INTEGER NOT NULL,
            note TEXT NOT NULL DEFAULT '',
            updated_at INTEGER NOT NULL DEFAULT 0
        )
        """
    )


def read_attr_overrides(analysis_db: Path | None) -> dict[str, int]:
    if analysis_db is None or not analysis_db.exists():
        return {}
    con = sqlite3.connect(analysis_db, timeout=15)
    try:
        ensure_override_table(con)
        rows = con.execute("SELECT sha256, attr FROM chart_attr_override").fetchall()
    finally:
        con.close()
    return {str(sha).lower(): int(attr) for sha, attr in rows}


def set_attr_override(analysis_db: Path, sha256: str, attr: int | None, note: str = "") -> None:
    """手動属性を設定する。attr=None で補正を削除する。"""
    con = sqlite3.connect(analysis_db, timeout=15)
    try:
        ensure_override_table(con)
        if attr is None:
            con.execute("DELETE FROM chart_attr_override WHERE sha256 = ?", (sha256.lower(),))
        else:
            con.execute(
                "INSERT INTO chart_attr_override(sha256, attr, note, updated_at) VALUES (?,?,?,?) "
                "ON CONFLICT(sha256) DO UPDATE SET attr=excluded.attr, note=excluded.note, "
                "updated_at=excluded.updated_at",
                (sha256.lower(), int(attr), note, int(time.time())),
            )
        con.commit()
    finally:
        con.close()


# ---------------------------------------------------------------------------
# 属性別プレイレポート (scoredatalog.db優先、scorelog.db互換)
# ---------------------------------------------------------------------------

def _read_play_log_rows(playlog_db: Path) -> tuple[list[tuple[str, int, int | None]], str]:
    """プレイ履歴DBから (sha256, date, notes) を読む。

    scoredatalog は全プレイ履歴として優先する。旧版との互換のため scorelog も
    読めるが、scorelog は自己ベスト更新時だけ記録される環境があるため、練習量
    集計には scoredatalog.db を推奨する。
    """
    con = sqlite3.connect(playlog_db, timeout=15)
    try:
        tables = {
            str(row[0]).lower()
            for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if "scoredatalog" in tables:
            table = "scoredatalog"
            source = "scoredatalog"
        elif "scorelog" in tables:
            table = "scorelog"
            source = "scorelog"
        else:
            raise ValueError(
                "プレイ履歴テーブルが見つかりません "
                "(scoredatalog または scorelog が必要です)"
            )
        columns = {
            str(row[1]).lower()
            for row in con.execute(f"PRAGMA table_info({table})")
        }
        if "sha256" not in columns or "date" not in columns:
            raise ValueError(f"{table} に sha256/date カラムがありません")
        notes_expr = "notes" if "notes" in columns else "NULL"
        raw_rows = con.execute(
            f"SELECT sha256, date, {notes_expr} AS notes FROM {table}"
        ).fetchall()
    finally:
        con.close()
    rows: list[tuple[str, int, int | None]] = []
    for sha, date, notes in raw_rows:
        if not sha:
            continue
        try:
            date_value = int(date or 0)
        except (TypeError, ValueError):
            continue
        try:
            notes_value = int(notes) if notes is not None else None
        except (TypeError, ValueError):
            notes_value = None
        rows.append((str(sha).lower(), date_value, notes_value))
    return rows, source


def build_attr_play_report(
    playlog_db: Path,
    analyses: dict[str, dict[str, Any]],
    overrides: dict[str, int] | None = None,
    now: int | None = None,
    windows: tuple[int, ...] = (7, 28),
    target_share: dict[str, float] | None = None,
) -> str:
    """全プレイ履歴と解析結果から属性別練習配分レポートを作る。

    scoredatalog.db を推奨する。scorelog.dbも互換入力として読めるが、環境に
    よっては自己ベスト更新プレイしか含まれないため、レポート内へ警告を出す。
    プレイ時間は譜面の演奏時間(chart_seconds)の合計で近似する。
    target_share は {"16分乱打": 40.0} のような目標配分(%)。
    """
    from datetime import datetime

    now = now or int(time.time())
    overrides = overrides or {}
    rows, source = _read_play_log_rows(playlog_db)

    def attr_of(sha: str) -> int | None:
        sha = sha.lower()
        if sha in overrides:
            return overrides[sha]
        row = analyses.get(sha)
        if not row or str(row.get("error") or ""):
            return None
        attr = row.get("attr")
        return int(attr) if attr is not None else None

    lines: list[str] = ["属性別練習配分レポート", ""]
    lines.append(f"履歴ソース: {source}")
    if source == "scorelog":
        lines.append(
            "注意: scorelogは更新プレイのみ記録される場合があります。"
            "正確な練習量にはscoredatalog.dbを指定してください。"
        )
    lines.append("")
    order = [1, 2, 3, 5, 4, 0]
    daily_attr: dict[int, set[int]] = defaultdict(set)
    max_window = max(windows) if windows else 0
    for window in windows:
        threshold = now - window * 86400
        seconds: dict[int, float] = defaultdict(float)
        plays: dict[int, int] = defaultdict(int)
        notes: dict[int, int] = defaultdict(int)
        unknown_plays = 0
        for sha, date, logged_notes in rows:
            if date < threshold:
                continue
            attr = attr_of(sha)
            if attr is None:
                unknown_plays += 1
                continue
            row = analyses.get(sha, {})
            seconds[attr] += float(row.get("chart_seconds") or 120.0)
            if logged_notes is not None and logged_notes > 0:
                notes[attr] += logged_notes
            else:
                notes[attr] += int(row.get("total_notes") or 0)
            plays[attr] += 1
            if window == max_window:
                # ローカルタイムの日付で区切り、JST早朝が前日扱いになるのを防ぐ。
                local_day = datetime.fromtimestamp(date).date().toordinal()
                daily_attr[local_day].add(attr)
        total_seconds = sum(seconds.values())
        lines.append(f"■ 直近{window}日")
        if total_seconds <= 0:
            lines.append("  解析済み譜面のプレイ記録がありません")
            lines.append("")
            continue
        for code in order:
            if plays.get(code, 0) == 0:
                continue
            share = seconds[code] / total_seconds * 100.0
            minutes = seconds[code] / 60.0
            label = ATTR_LABELS.get(code, str(code))
            marker = ""
            if target_share and label in target_share:
                gap = share - target_share[label]
                if gap <= -15.0:
                    marker = f"  ← 目標{target_share[label]:.0f}%より{-gap:.0f}pt不足"
                elif gap >= 15.0:
                    marker = f"  ← 目標{target_share[label]:.0f}%より{gap:.0f}pt過多"
            lines.append(
                f"  {label}: {share:5.1f}%  {minutes:6.1f}分  {plays[code]:4d}プレイ"
                f"  {notes[code]:,}ノーツ{marker}"
            )
        if unknown_plays:
            lines.append(f"  (未解析・解析対象外: {unknown_plays}プレイ)")
        lines.append("")

    # 同一属性だけを練習した連続日数。日付はOSのローカルタイムで判定する。
    if daily_attr:
        days = sorted(daily_attr, reverse=True)
        streak_attr: int | None = None
        streak = 0
        for offset, day in enumerate(days):
            if offset > 0 and days[offset - 1] - day != 1:
                break
            attrs = daily_attr[day]
            if len(attrs) == 1:
                only = next(iter(attrs))
                if streak_attr is None or streak_attr == only:
                    streak_attr = only
                    streak += 1
                    continue
            break
        if streak_attr is not None and streak >= 3:
            lines.append(
                f"注意: {ATTR_LABELS.get(streak_attr)}のみの練習日が{streak}日連続しています。"
                "属性ローテーションを検討してください。"
            )
            lines.append("")
    return "\n".join(lines)
