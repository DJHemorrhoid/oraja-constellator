from __future__ import annotations

import json
import logging
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import tempfile
import traceback
import webbrowser
from pathlib import Path
from typing import Any, Callable

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, simpledialog, ttk
except Exception as exc:  # pragma: no cover
    print(f"Tkinterを読み込めません: {exc}", file=sys.stderr)
    raise

from core import (
    APP_NAME,
    APP_VERSION,
    MANAGED_ROOT_NAME,
    CLEAR_TYPES,
    DENSITY_RANK_VALUES,
    FIELD_DEFS,
    FILTER_CATEGORIES,
    RANK_VALUES,
    RHYTHM_VALUES,
    ATTR_VALUE_CHOICES,
    SUBCAT_VALUE_CHOICES,
    AppSettings,
    AppTableURL,
    TableCombination,
    DefaultFolderEntry,
    Condition,
    EnvironmentPaths,
    Preset,
    ProgressUpdate,
    TableInfo,
    ToolError,
    AnalysisCancelled,
    analyze_selected_tables,
    analyze_song_database,
    active_default_folders,
    apply_default_folder_visibility,
    common_root_candidates,
    combination_method_label,
    combination_table_ids,
    combination_input_refs,
    combination_source_ref,
    table_source_ref,
    split_source_ref,
    validate_combination_graph,
    detect_environment,
    list_player_names,
    difficulty_registry_path,
    difficulty_registry_sources,
    difficulty_scale_summary,
    find_material_for_chart,
    generate_all,
    instant_density_profile_path,
    instant_density_profile_summary,
    load_settings,
    load_difficulty_scale_registry,
    load_instant_density_profiles,
    load_tables,
    ensure_table_combinations,
    sync_default_folder_catalog,
    make_rhythm_bpm_presets,
    merge_initial_filter_presets,
    match_difficulty_scale,
    parse_remote_table,
    preview_counts,
    reclassify_chart_analysis,
    reanalyze_material_for_chart,
    register_table_url,
    save_settings,
    save_tool_cache,
    summarize_environment,
    table_id_for_url,
    update_chart_analysis,
    validate_default_json,
    write_filter_conditions,
    ensure_initial_songdata_backup,
    build_simple_runtime_settings,
    initialize_simple_selection,
    initialize_simple_filter_set_selection,
    simple_filter_sets,
    ordered_simple_filter_sets,
    simple_filter_set_name,
    simple_presets_for_set,
    simple_presets_for_sets,
    sync_filter_preset_packs,
    load_filter_preset_packs,
    merge_chart_analysis_db,
    analysis_db_diff_count,
)
from attribute_rule_pack import (
    FEATURE_SCHEMA_VERSION,
    get_active_rule_pack_info,
    load_rule_pack_file,
    reload_active_rule_pack,
)
from updater import (
    UpdateError,
    atomic_replace_with_backup,
    download_verified,
    fetch_manifest,
    is_newer,
    load_update_source,
    manifest_update_summary,
    version_key,
)

PYTHON_DIR = Path(__file__).resolve().parent
APP_DIR = PYTHON_DIR.parent
SCRIPT_DIR = APP_DIR
DATA_DIR = APP_DIR / "data"
CACHE_DIR = DATA_DIR / "table_cache"
CHART_ANALYSIS_DB = DATA_DIR / "chart_analysis.db"
PENDING_ANALYSIS_DB = DATA_DIR / "chart_analysis_pending.db"
SONGDATA_BACKUP_DIR = DATA_DIR / "backups" / "songdata"
SETTINGS_PATH = DATA_DIR / "settings.json"
LOG_DIR = APP_DIR / "logs"
LOG_PATH = LOG_DIR / "oraja_constellator.log"
FILTER_PACK_DIR = APP_DIR / "filter_packs"
ANALYSIS_RULE_PACK_DIR = APP_DIR / "analysis_packs"
UPDATE_DIR = APP_DIR / "update"
UPDATE_SOURCE_PATH = UPDATE_DIR / "update_source.json"
UPDATE_DOWNLOAD_DIR = DATA_DIR / "update_downloads"

LOG_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
LOGGER = logging.getLogger(APP_NAME)


def format_duration(seconds: float | int | None) -> str:
    if seconds is None:
        return "--:--"
    value = max(0, int(seconds))
    hours, value = divmod(value, 3600)
    minutes, secs = divmod(value, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def open_path(path: Path) -> None:
    path = path.expanduser().resolve()
    if not path.exists():
        raise ToolError(f"見つかりません: {path}")
    if os.name == "nt":
        os.startfile(str(path))  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


def open_url(url: str) -> None:
    url = (url or "").strip()
    if not url:
        raise ToolError("URLが設定されていません")
    webbrowser.open(url)


def is_rian_probably_running() -> bool:
    if os.name != "nt":
        return False
    try:
        output = subprocess.check_output(
            ["tasklist", "/FO", "CSV", "/NH"],
            text=True,
            encoding="cp932",
            errors="ignore",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).lower()
        return any(token in output for token in ("java.exe", "javaw.exe", "lr2oraja"))
    except Exception:
        return False


def focus_rian_window() -> bool:
    if os.name != "nt":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        titles = ("lr2oraja", "beatoraja", "endless dream", "rian")
        found: list[int] = []

        @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
        def enum_proc(hwnd, _lparam):
            if not user32.IsWindowVisible(hwnd):
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            title = buf.value.lower()
            if any(token in title for token in titles):
                found.append(hwnd)
                return False
            return True

        user32.EnumWindows(enum_proc, 0)
        if not found:
            return False
        hwnd = found[0]
        user32.ShowWindow(hwnd, 9)
        user32.SetForegroundWindow(hwnd)
        return True
    except Exception:
        LOGGER.exception("rian focus failed")
        return False


class ConditionDialog(tk.Toplevel):
    def __init__(self, parent: tk.Misc, initial: Condition | None = None):
        super().__init__(parent)
        self.title("条件編集")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()
        self.result: Condition | None = None
        initial = initial or Condition()
        self.condition_id = initial.condition_id
        initial_info = FIELD_DEFS.get(initial.field, FIELD_DEFS["played"])

        frame = ttk.Frame(self, padding=12)
        frame.grid(sticky="nsew")

        ttk.Label(frame, text="カテゴリ").grid(row=0, column=0, sticky="w", pady=4)
        self.category_var = tk.StringVar(value=CATEGORY_LABEL_BY_ID.get(initial_info.get("category", "record"), "プレイレコード"))
        self.category_combo = ttk.Combobox(
            frame, textvariable=self.category_var, state="readonly", width=25,
            values=[label for _key, label in FILTER_CATEGORIES],
        )
        self.category_combo.grid(row=0, column=1, sticky="ew", pady=4)

        ttk.Label(frame, text="項目").grid(row=1, column=0, sticky="w", pady=4)
        self.field_display_var = tk.StringVar(value=initial_info["label"])
        self.field_combo = ttk.Combobox(frame, textvariable=self.field_display_var, state="readonly", width=29)
        self.field_combo.grid(row=1, column=1, sticky="ew", pady=4)

        ttk.Label(frame, text="比較").grid(row=2, column=0, sticky="w", pady=4)
        self.op_var = tk.StringVar(value=initial.op)
        self.op_combo = ttk.Combobox(frame, textvariable=self.op_var, state="readonly", width=12)
        self.op_combo.grid(row=2, column=1, sticky="w", pady=4)

        ttk.Label(frame, text="値").grid(row=3, column=0, sticky="w", pady=4)
        self.value_var = tk.StringVar(value=initial.value)
        self.value_combo = ttk.Combobox(frame, textvariable=self.value_var, width=25)
        self.value_combo.grid(row=3, column=1, sticky="ew", pady=4)

        self.enabled_var = tk.BooleanVar(value=initial.enabled)
        ttk.Checkbutton(frame, text="この条件を有効にする", variable=self.enabled_var).grid(
            row=4, column=0, columnspan=2, sticky="w", pady=6
        )

        buttons = ttk.Frame(frame)
        buttons.grid(row=5, column=0, columnspan=2, sticky="e", pady=(10, 0))
        ttk.Button(buttons, text="OK", command=self.on_ok).pack(side="left", padx=4)
        ttk.Button(buttons, text="キャンセル", command=self.destroy).pack(side="left", padx=4)

        self.category_combo.bind("<<ComboboxSelected>>", lambda _e: self.refresh_category())
        self.field_combo.bind("<<ComboboxSelected>>", lambda _e: self.refresh_field())
        self.refresh_category(keep_field=True)
        self.refresh_field(keep_value=True)
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.wait_visibility()
        self.focus_set()

    def fields_in_category(self) -> list[str]:
        category_id = CATEGORY_ID_BY_LABEL.get(self.category_var.get(), "record")
        return [key for key, info in FIELD_DEFS.items() if info.get("category") == category_id]

    def selected_field(self) -> str:
        display = self.field_display_var.get()
        for key in self.fields_in_category():
            if FIELD_DEFS[key]["label"] == display:
                return key
        fields = self.fields_in_category()
        return fields[0] if fields else "played"

    def refresh_category(self, keep_field: bool = False) -> None:
        fields = self.fields_in_category()
        labels = [FIELD_DEFS[key]["label"] for key in fields]
        current = self.field_display_var.get()
        self.field_combo.configure(values=labels)
        if not keep_field or current not in labels:
            self.field_display_var.set(labels[0] if labels else "")
        self.refresh_field(keep_value=keep_field)

    def refresh_field(self, keep_value: bool = False) -> None:
        key = self.selected_field()
        info = FIELD_DEFS.get(key, FIELD_DEFS["played"])
        self.op_combo.configure(values=info["ops"])
        if self.op_var.get() not in info["ops"]:
            self.op_var.set(info["ops"][0])
        value_type = info["type"]
        values: list[str] = []
        state = "normal"
        if value_type == "bool":
            values, state = ["true", "false"], "readonly"
        elif value_type == "clear":
            values, state = list(CLEAR_TYPES.keys()), "readonly"
        elif value_type == "rank":
            values, state = list(RANK_VALUES.keys()), "readonly"
        elif value_type == "density_rank":
            values, state = DENSITY_RANK_VALUES, "readonly"
        elif value_type == "rhythm":
            values, state = RHYTHM_VALUES, "readonly"
        elif value_type == "attr":
            values, state = ATTR_VALUE_CHOICES, "readonly"
        elif value_type == "attr_sub":
            values, state = SUBCAT_VALUE_CHOICES, "readonly"
        self.value_combo.configure(state=state, values=values)
        if not keep_value or (values and self.value_var.get() not in values):
            self.value_var.set(info["default"])

    def on_ok(self) -> None:
        field_name = self.selected_field()
        if field_name not in FIELD_DEFS:
            messagebox.showerror(APP_NAME, "項目が不正です", parent=self)
            return
        value = self.value_var.get().strip()
        if FIELD_DEFS[field_name]["type"] not in {"text"} and not value:
            messagebox.showerror(APP_NAME, "値を入力してください", parent=self)
            return
        try:
            if FIELD_DEFS[field_name]["type"] == "int":
                int(float(value))
            elif FIELD_DEFS[field_name]["type"] == "float":
                float(value)
        except ValueError:
            messagebox.showerror(APP_NAME, "数値を入力してください", parent=self)
            return
        self.result = Condition(
            field=field_name,
            op=self.op_var.get(),
            value=value,
            enabled=self.enabled_var.get(),
            condition_id=self.condition_id,
        )
        self.destroy()


class MultiConditionDialog(tk.Toplevel):
    def __init__(self, parent: tk.Misc):
        super().__init__(parent)
        self.title("条件をまとめて追加")
        self.geometry("660x520")
        self.transient(parent)
        self.grab_set()
        self.result: list[Condition] = []
        outer = ttk.Frame(self, padding=12)
        outer.pack(fill="both", expand=True)
        ttk.Label(
            outer,
            text="追加する項目をクリックして選択してください。複数カテゴリから同時に選べます。",
        ).pack(anchor="w")
        self.tree = ttk.Treeview(outer, show="tree", selectmode="extended")
        self.tree.pack(fill="both", expand=True, pady=8)
        for category_id, category_label in FILTER_CATEGORIES:
            parent_id = f"cat:{category_id}"
            self.tree.insert("", "end", iid=parent_id, text=category_label, open=True)
            for field_name, info in FIELD_DEFS.items():
                if info.get("category") == category_id:
                    self.tree.insert(parent_id, "end", iid=f"field:{field_name}", text=info["label"])
        self.tree.bind("<Button-1>", self.toggle_item, add="+")
        tools = ttk.Frame(outer)
        tools.pack(fill="x")
        ttk.Button(tools, text="全項目を選択", command=self.select_all).pack(side="left", padx=3)
        ttk.Button(tools, text="選択解除", command=lambda: self.tree.selection_remove(self.tree.selection())).pack(side="left", padx=3)
        ttk.Button(tools, text="追加", command=self.on_ok).pack(side="right", padx=3)
        ttk.Button(tools, text="キャンセル", command=self.destroy).pack(side="right", padx=3)

    def toggle_item(self, event) -> str | None:
        item = self.tree.identify_row(event.y)
        if not item or not item.startswith("field:"):
            return None
        selected = set(self.tree.selection())
        if item in selected:
            self.tree.selection_remove(item)
        else:
            self.tree.selection_add(item)
        self.tree.focus(item)
        return "break"

    def select_all(self) -> None:
        items = []
        for category_id, _label in FILTER_CATEGORIES:
            items.extend(self.tree.get_children(f"cat:{category_id}"))
        self.tree.selection_set(items)

    def on_ok(self) -> None:
        fields = [item.split(":", 1)[1] for item in self.tree.selection() if item.startswith("field:")]
        if not fields:
            messagebox.showinfo(APP_NAME, "追加する条件を選択してください", parent=self)
            return
        self.result = [
            Condition(field=field_name, op=FIELD_DEFS[field_name]["ops"][0], value=FIELD_DEFS[field_name]["default"])
            for field_name in fields
        ]
        self.destroy()


class PresetDialog(tk.Toplevel):
    def __init__(self, parent: tk.Misc, preset: Preset | None = None):
        super().__init__(parent)
        self.title("フィルタープリセット編集")
        self.geometry("820x560")
        self.transient(parent)
        self.grab_set()
        self.result: Preset | None = None
        source = preset or Preset()
        self.working = Preset(
            preset_id=source.preset_id,
            name=source.name,
            visible=source.visible,
            join=source.join,
            conditions=[Condition(c.field, c.op, c.value, c.enabled, c.condition_id) for c in source.conditions],
            group_name=source.group_name,
        )

        outer = ttk.Frame(self, padding=12)
        outer.pack(fill="both", expand=True)
        top = ttk.Frame(outer)
        top.pack(fill="x")
        ttk.Label(top, text="名称").grid(row=0, column=0, sticky="w")
        self.name_var = tk.StringVar(value=self.working.name)
        ttk.Entry(top, textvariable=self.name_var, width=36).grid(row=0, column=1, sticky="ew", padx=8)
        self.visible_var = tk.BooleanVar(value=self.working.visible)
        ttk.Checkbutton(top, text="選曲画面に表示", variable=self.visible_var).grid(row=0, column=2, padx=8)
        ttk.Label(top, text="親グループ（任意）").grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.group_var = tk.StringVar(value=self.working.group_name)
        ttk.Entry(top, textvariable=self.group_var, width=36).grid(row=1, column=1, sticky="ew", padx=8, pady=(8, 0))
        ttk.Label(top, text="同じ親グループ名のプリセットを一段まとめます", foreground="#666666").grid(
            row=1, column=2, sticky="w", padx=8, pady=(8, 0)
        )
        ttk.Label(top, text="条件結合").grid(row=2, column=0, sticky="w", pady=(8, 0))
        self.join_var = tk.StringVar(value=self.working.join)
        ttk.Combobox(top, textvariable=self.join_var, values=["AND", "OR"], state="readonly", width=8).grid(
            row=2, column=1, sticky="w", padx=8, pady=(8, 0)
        )
        top.columnconfigure(1, weight=1)

        columns = ("enabled", "category", "field", "op", "value")
        self.tree = ttk.Treeview(outer, columns=columns, show="headings", height=12)
        self.tree.heading("enabled", text="有効")
        self.tree.heading("category", text="カテゴリ")
        self.tree.heading("field", text="項目")
        self.tree.heading("op", text="比較")
        self.tree.heading("value", text="値")
        self.tree.column("enabled", width=55, anchor="center")
        self.tree.column("category", width=120)
        self.tree.column("field", width=210)
        self.tree.column("op", width=70, anchor="center")
        self.tree.column("value", width=150)
        self.tree.pack(fill="both", expand=True, pady=10)
        self.tree.bind("<Double-1>", lambda _e: self.edit_condition())

        tools = ttk.Frame(outer)
        tools.pack(fill="x")
        ttk.Button(tools, text="条件追加", command=self.add_condition).pack(side="left", padx=3)
        ttk.Button(tools, text="複数条件を追加", command=self.add_conditions_bulk).pack(side="left", padx=3)
        ttk.Button(tools, text="編集", command=self.edit_condition).pack(side="left", padx=3)
        ttk.Button(tools, text="削除", command=self.delete_condition).pack(side="left", padx=3)
        ttk.Button(tools, text="↑", width=4, command=lambda: self.move_condition(-1)).pack(side="left", padx=3)
        ttk.Button(tools, text="↓", width=4, command=lambda: self.move_condition(1)).pack(side="left", padx=3)
        ttk.Button(tools, text="OK", command=self.on_ok).pack(side="right", padx=3)
        ttk.Button(tools, text="キャンセル", command=self.destroy).pack(side="right", padx=3)
        self.refresh_tree()
        self.protocol("WM_DELETE_WINDOW", self.destroy)

    def refresh_tree(self) -> None:
        self.tree.delete(*self.tree.get_children())
        for index, c in enumerate(self.working.conditions):
            info = FIELD_DEFS.get(c.field, {})
            label = info.get("label", c.field)
            category = CATEGORY_LABEL_BY_ID.get(info.get("category", ""), "その他")
            self.tree.insert("", "end", iid=str(index), values=("✓" if c.enabled else "—", category, label, c.op, c.value))

    def selected_index(self) -> int | None:
        sel = self.tree.selection()
        return int(sel[0]) if sel else None

    def add_condition(self) -> None:
        dialog = ConditionDialog(self)
        self.wait_window(dialog)
        if dialog.result:
            self.working.conditions.append(dialog.result)
            self.refresh_tree()

    def add_conditions_bulk(self) -> None:
        dialog = MultiConditionDialog(self)
        self.wait_window(dialog)
        if dialog.result:
            existing = {c.field for c in self.working.conditions}
            added = [c for c in dialog.result if c.field not in existing]
            self.working.conditions.extend(added)
            self.refresh_tree()

    def edit_condition(self) -> None:
        index = self.selected_index()
        if index is None:
            return
        dialog = ConditionDialog(self, self.working.conditions[index])
        self.wait_window(dialog)
        if dialog.result:
            self.working.conditions[index] = dialog.result
            self.refresh_tree()
            self.tree.selection_set(str(index))

    def delete_condition(self) -> None:
        index = self.selected_index()
        if index is None:
            return
        del self.working.conditions[index]
        self.refresh_tree()

    def move_condition(self, delta: int) -> None:
        index = self.selected_index()
        if index is None:
            return
        new_index = index + delta
        if not (0 <= new_index < len(self.working.conditions)):
            return
        self.working.conditions[index], self.working.conditions[new_index] = (
            self.working.conditions[new_index],
            self.working.conditions[index],
        )
        self.refresh_tree()
        self.tree.selection_set(str(new_index))

    def on_ok(self) -> None:
        name = self.name_var.get().strip()
        if not name:
            messagebox.showerror(APP_NAME, "名称を入力してください", parent=self)
            return
        self.working.name = name
        self.working.visible = self.visible_var.get()
        self.working.join = self.join_var.get()
        self.working.group_name = self.group_var.get().strip()
        self.result = self.working
        self.destroy()


class RhythmBpmBulkDialog(tk.Toplevel):
    def __init__(self, parent: tk.Misc):
        super().__init__(parent)
        self.title("16分／12分 × 実質BPM 一括生成")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()
        self.result: list[Preset] | None = None

        frame = ttk.Frame(self, padding=14)
        frame.grid(sticky="nsew")
        ttk.Label(frame, text="16分系の親グループ").grid(row=0, column=0, sticky="w", pady=4)
        self.group16_var = tk.StringVar(value="16分主体")
        ttk.Entry(frame, textvariable=self.group16_var, width=34).grid(row=0, column=1, sticky="ew", pady=4)
        ttk.Label(frame, text="12分系の親グループ").grid(row=1, column=0, sticky="w", pady=4)
        self.group12_var = tk.StringVar(value="12分主体")
        ttk.Entry(frame, textvariable=self.group12_var, width=34).grid(row=1, column=1, sticky="ew", pady=4)
        ttk.Label(frame, text="実質BPMの区切り").grid(row=2, column=0, sticky="w", pady=4)
        self.cutoffs_var = tk.StringVar(value="120, 140, 160, 180, 200")
        ttk.Entry(frame, textvariable=self.cutoffs_var, width=34).grid(row=2, column=1, sticky="ew", pady=4)
        ttk.Label(
            frame,
            text="カンマ区切り・昇順。例では各親グループに6帯、合計12プリセットを作成します。",
            foreground="#666666",
        ).grid(row=3, column=0, columnspan=2, sticky="w", pady=(2, 10))
        self.preview_var = tk.StringVar()
        ttk.Label(frame, textvariable=self.preview_var).grid(row=4, column=0, columnspan=2, sticky="w", pady=4)
        self.cutoffs_var.trace_add("write", lambda *_: self.update_preview())
        self.group16_var.trace_add("write", lambda *_: self.update_preview())
        self.group12_var.trace_add("write", lambda *_: self.update_preview())

        buttons = ttk.Frame(frame)
        buttons.grid(row=5, column=0, columnspan=2, sticky="e", pady=(10, 0))
        ttk.Button(buttons, text="一括生成", command=self.on_ok).pack(side="left", padx=4)
        ttk.Button(buttons, text="キャンセル", command=self.destroy).pack(side="left", padx=4)
        self.update_preview()

    def parse_cutoffs(self) -> list[float]:
        values = []
        for token in self.cutoffs_var.get().replace("、", ",").split(","):
            token = token.strip()
            if token:
                values.append(float(token))
        return values

    def update_preview(self) -> None:
        try:
            presets = make_rhythm_bpm_presets(
                self.parse_cutoffs(), self.group16_var.get(), self.group12_var.get()
            )
            per_group = len(presets) // 2
            self.preview_var.set(f"生成予定: {per_group}帯 × 2系統 = {len(presets)}プリセット")
        except Exception as exc:
            self.preview_var.set(f"入力確認: {exc}")

    def on_ok(self) -> None:
        try:
            self.result = make_rhythm_bpm_presets(
                self.parse_cutoffs(), self.group16_var.get(), self.group12_var.get()
            )
        except (ValueError, ToolError) as exc:
            messagebox.showerror(APP_NAME, str(exc), parent=self)
            return
        self.destroy()


class AddTableDialog(tk.Toplevel):
    def __init__(self, parent: tk.Misc):
        super().__init__(parent)
        self.title("難易度表URL追加")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()
        self.result: AppTableURL | None = None
        frame = ttk.Frame(self, padding=12)
        frame.grid(sticky="nsew")
        ttk.Label(frame, text="表URL").grid(row=0, column=0, sticky="w", pady=4)
        self.url_var = tk.StringVar()
        ttk.Entry(frame, textvariable=self.url_var, width=64).grid(row=0, column=1, sticky="ew", pady=4)
        ttk.Label(frame, text="header JSON URL\n（通常は空欄）").grid(row=1, column=0, sticky="w", pady=4)
        self.header_var = tk.StringVar()
        ttk.Entry(frame, textvariable=self.header_var, width=64).grid(row=1, column=1, sticky="ew", pady=4)
        self.register_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            frame,
            text="beatorajaのconfig_sys.jsonにも登録する",
            variable=self.register_var,
        ).grid(row=2, column=0, columnspan=2, sticky="w", pady=8)
        ttk.Label(
            frame,
            text="登録時はconfig_sys.jsonをバックアップします。beatoraja停止中の操作を推奨します。",
            foreground="#666666",
        ).grid(row=3, column=0, columnspan=2, sticky="w")
        buttons = ttk.Frame(frame)
        buttons.grid(row=4, column=0, columnspan=2, sticky="e", pady=(12, 0))
        ttk.Button(buttons, text="追加", command=self.on_ok).pack(side="left", padx=4)
        ttk.Button(buttons, text="キャンセル", command=self.destroy).pack(side="left", padx=4)

    def on_ok(self) -> None:
        url = self.url_var.get().strip()
        if not url.startswith(("http://", "https://")):
            messagebox.showerror(APP_NAME, "http:// または https:// のURLを入力してください", parent=self)
            return
        self.result = AppTableURL(
            url=url,
            header_url=self.header_var.get().strip(),
            register_to_beatoraja=self.register_var.get(),
        )
        self.destroy()


class CombinationDialog(tk.Toplevel):
    METHOD_LABEL_TO_ID = {
        "先頭入力をそのまま": "plain",
        "全入力に共通する譜面": "intersect",
        "先頭入力から他入力を除外": "exclude_others",
        "先頭入力＋他入力の同番号レベル": "union_same",
        "全入力を同番号レベルで集約": "merge_same",
        "所属レベルの組み合わせごとに分類": "level_signature",
    }

    def __init__(
        self,
        parent: tk.Misc,
        tables: list[TableInfo],
        combinations: list[TableCombination],
        source: TableCombination | None = None,
    ):
        super().__init__(parent)
        self.title("掛け合わせ済み難易度表")
        self.geometry("760x620")
        self.minsize(700, 540)
        self.transient(parent)
        self.grab_set()
        self.result: TableCombination | None = None
        self.source_id = source.combination_id if source else ""
        self.selected_refs: list[str] = []
        self.display_to_ref: dict[str, str] = {}
        self.ref_to_display: dict[str, str] = {}

        valid_tables = [table for table in tables if not table.error and table.levels]
        for table in valid_tables:
            ref = table_source_ref(table.table_id)
            display = f"元表｜{table.name}  [{table.table_id[:8]}]"
            self.display_to_ref[display] = ref
            self.ref_to_display[ref] = display
        for combo in combinations:
            if combo.combination_id == self.source_id:
                continue
            ref = combination_source_ref(combo.combination_id)
            display = f"生成済み｜{combo.name}  [{combo.combination_id[:8]}]"
            self.display_to_ref[display] = ref
            self.ref_to_display[ref] = display

        src = source or TableCombination()
        self.selected_refs = [ref for ref in combination_input_refs(src) if ref != combination_source_ref(self.source_id)]

        frame = ttk.Frame(self, padding=12)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(0, weight=1)
        frame.columnconfigure(2, weight=1)
        frame.rowconfigure(3, weight=1)

        ttk.Label(frame, text="フォルダ名").grid(row=0, column=0, sticky="w", pady=5)
        self.name_var = tk.StringVar(value=src.name)
        ttk.Entry(frame, textvariable=self.name_var).grid(row=0, column=1, columnspan=2, sticky="ew", pady=5)

        ttk.Label(frame, text="掛け合わせ方").grid(row=1, column=0, sticky="w", pady=5)
        initial_method = next((label for label, value in self.METHOD_LABEL_TO_ID.items() if value == src.method), "先頭入力をそのまま")
        self.method_var = tk.StringVar(value=initial_method)
        self.method_combo = ttk.Combobox(
            frame, textvariable=self.method_var, state="readonly",
            values=list(self.METHOD_LABEL_TO_ID),
        )
        self.method_combo.grid(row=1, column=1, columnspan=2, sticky="ew", pady=5)
        self.method_combo.bind("<<ComboboxSelected>>", lambda _e: self.update_method_note())

        ttk.Label(frame, text="入力候補").grid(row=2, column=0, sticky="w", pady=(8, 3))
        ttk.Label(frame, text="入力順（表記・基準順）").grid(row=2, column=2, sticky="w", pady=(8, 3))

        available_frame = ttk.Frame(frame)
        available_frame.grid(row=3, column=0, sticky="nsew", padx=(0, 8))
        available_frame.rowconfigure(0, weight=1)
        available_frame.columnconfigure(0, weight=1)
        self.available_list = tk.Listbox(available_frame, selectmode="extended", exportselection=False)
        self.available_list.grid(row=0, column=0, sticky="nsew")
        ascroll = ttk.Scrollbar(available_frame, orient="vertical", command=self.available_list.yview)
        ascroll.grid(row=0, column=1, sticky="ns")
        self.available_list.configure(yscrollcommand=ascroll.set)
        for display in self.display_to_ref:
            self.available_list.insert("end", display)
        self.available_list.bind("<Double-1>", lambda _e: self.add_selected_inputs())

        right_frame = ttk.Frame(frame)
        right_frame.grid(row=3, column=2, sticky="nsew")
        right_frame.rowconfigure(0, weight=1)
        right_frame.columnconfigure(0, weight=1)
        self.selected_list = tk.Listbox(right_frame, selectmode="extended", exportselection=False)
        self.selected_list.grid(row=0, column=0, sticky="nsew")
        sscroll = ttk.Scrollbar(right_frame, orient="vertical", command=self.selected_list.yview)
        sscroll.grid(row=0, column=1, sticky="ns")
        self.selected_list.configure(yscrollcommand=sscroll.set)
        self.selected_list.bind("<Double-1>", lambda _e: self.remove_selected_inputs())

        middle = ttk.Frame(frame)
        middle.grid(row=3, column=1, sticky="ns", padx=4)
        ttk.Button(middle, text="追加 →", command=self.add_selected_inputs).pack(pady=(45, 6))
        ttk.Button(middle, text="← 除外", command=self.remove_selected_inputs).pack(pady=6)
        ttk.Button(middle, text="↑", width=7, command=lambda: self.move_input(-1)).pack(pady=(28, 5))
        ttk.Button(middle, text="↓", width=7, command=lambda: self.move_input(1)).pack(pady=5)

        self.method_note_var = tk.StringVar()
        ttk.Label(
            frame, textvariable=self.method_note_var, foreground="#555555", wraplength=710,
        ).grid(row=4, column=0, columnspan=3, sticky="w", pady=(8, 2))

        self.visible_var = tk.BooleanVar(value=src.visible)
        ttk.Checkbutton(frame, text="default.jsonへ表示する", variable=self.visible_var).grid(
            row=5, column=0, columnspan=3, sticky="w", pady=(7, 2)
        )
        ttk.Label(
            frame,
            text=(
                "非表示の生成済み表も、別の生成済み表の入力として利用できます。"
                "多段合成は依存順に自動再計算され、循環参照は保存時に拒否されます。"
            ),
            foreground="#666666", wraplength=710,
        ).grid(row=6, column=0, columnspan=3, sticky="w", pady=3)

        buttons = ttk.Frame(frame)
        buttons.grid(row=7, column=0, columnspan=3, sticky="e", pady=(12, 0))
        ttk.Button(buttons, text="保存", command=self.on_ok).pack(side="left", padx=4)
        ttk.Button(buttons, text="キャンセル", command=self.destroy).pack(side="left", padx=4)
        self.refresh_selected_list()
        self.update_method_note()

    def refresh_selected_list(self) -> None:
        self.selected_list.delete(0, "end")
        for ref in self.selected_refs:
            display = self.ref_to_display.get(ref)
            if display is None:
                kind, value = split_source_ref(ref)
                display = f"未取得{kind}｜{value}"
            self.selected_list.insert("end", display)

    def add_selected_inputs(self) -> None:
        for index in self.available_list.curselection():
            display = self.available_list.get(index)
            ref = self.display_to_ref.get(display, "")
            if ref and ref not in self.selected_refs:
                self.selected_refs.append(ref)
        self.refresh_selected_list()

    def remove_selected_inputs(self) -> None:
        indices = list(self.selected_list.curselection())
        for index in reversed(indices):
            if 0 <= index < len(self.selected_refs):
                self.selected_refs.pop(index)
        self.refresh_selected_list()

    def move_input(self, delta: int) -> None:
        indices = list(self.selected_list.curselection())
        if len(indices) != 1:
            return
        index = indices[0]
        target = index + delta
        if not (0 <= target < len(self.selected_refs)):
            return
        self.selected_refs[index], self.selected_refs[target] = self.selected_refs[target], self.selected_refs[index]
        self.refresh_selected_list()
        self.selected_list.selection_set(target)

    def update_method_note(self) -> None:
        method = self.METHOD_LABEL_TO_ID.get(self.method_var.get(), "plain")
        notes = {
            "plain": "先頭の1表をそのまま出力します。",
            "intersect": "先頭入力のレベル構造を使い、全入力に所属する譜面だけを残します。",
            "exclude_others": "先頭入力のレベル構造を使い、2件目以降のいずれかに所属する譜面を除外します。A − (B ∪ C …)です。",
            "union_same": (
                "先頭入力のレベル構造を使い、他の全入力から同じ系列・同じ番号のレベルを追加します。"
                "sl・Ude・乱打・微縦連・/// と st・腕・dl・重発狂・連打複合は別系列として扱います。"
            ),
            "merge_same": "全入力の同じ番号のレベルを1つへまとめます。個人難易度表の集約向けです。",
            "level_signature": (
                "全入力の譜面を和集合で集め、譜面ごとの所属レベル構成で分類します。"
                "入力順がフォルダ名と並び順に反映されます。例: st0,腕1。"
            ),
        }
        self.method_note_var.set(notes.get(method, ""))

    def on_ok(self) -> None:
        name = self.name_var.get().strip()
        method = self.METHOD_LABEL_TO_ID.get(self.method_var.get(), "plain")
        refs = list(self.selected_refs)
        if not name:
            messagebox.showerror(APP_NAME, "フォルダ名を入力してください", parent=self)
            return
        required = 1 if method == "plain" else 2
        if len(refs) < required:
            messagebox.showerror(APP_NAME, f"この掛け合わせ方には入力が{required}件以上必要です", parent=self)
            return
        if method == "plain":
            refs = refs[:1]
        self.result = TableCombination(
            combination_id=self.source_id or os.urandom(16).hex(),
            name=name,
            visible=self.visible_var.get(),
            input_refs=refs,
            method=method,
        )
        self.destroy()


class ScrollableFrame(ttk.Frame):
    """A compact vertically scrollable ttk frame with pointer-local wheel handling."""

    def __init__(self, parent: tk.Misc, *, height: int = 360):
        super().__init__(parent)
        self.canvas = tk.Canvas(self, highlightthickness=0, height=height)
        self.scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.content = ttk.Frame(self.canvas)
        self.window_id = self.canvas.create_window((0, 0), window=self.content, anchor="nw")
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.scrollbar.pack(side="right", fill="y")
        self.content.bind("<Configure>", self._sync_region)
        self.canvas.bind("<Configure>", self._sync_width)
        # bind_all is needed so Entry/Checkbutton children also scroll, but the
        # callback must verify that the pointer is actually inside this frame.
        self.canvas.bind_all("<MouseWheel>", self._on_mousewheel, add="+")

    def _sync_region(self, _event=None) -> None:
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _sync_width(self, event) -> None:
        self.canvas.itemconfigure(self.window_id, width=max(1, event.width))

    def _contains_widget(self, widget: tk.Misc | None) -> bool:
        current = widget
        while current is not None:
            if current is self:
                return True
            current = getattr(current, "master", None)
        return False

    def _on_mousewheel(self, event):
        try:
            if not self.winfo_ismapped():
                return None
            target = self.winfo_containing(event.x_root, event.y_root)
            if not self._contains_widget(target):
                return None
            delta = int(-event.delta / 120)
            if delta == 0:
                delta = -1 if event.delta > 0 else 1
            self.canvas.yview_scroll(delta, "units")
            return "break"
        except Exception:
            return None


def bind_local_mousewheel(widget: tk.Misc, scroll_command: Callable[[int, str], Any]) -> None:
    """Keep mouse-wheel input inside the list under the pointer.

    Returning ``break`` prevents the editor canvas' global wheel binding from
    receiving a Treeview/Listbox event and scrolling a different region.
    """
    def _scroll(event: tk.Event):
        try:
            delta = int(-event.delta / 120)
            if delta == 0:
                delta = -1 if event.delta > 0 else 1
            scroll_command(delta, "units")
        except Exception:
            pass
        return "break"

    widget.bind("<MouseWheel>", _scroll, add="+")


class FilterListEditor(ttk.Frame):
    """Edit filter visibility and numeric values for the selected detail set."""

    NUMERIC_TYPES = {"int", "float"}

    def __init__(self, parent: tk.Misc, app: "MainApp", *, compact: bool = False):
        super().__init__(parent)
        self.app = app
        self.compact = compact
        self.filter_sets: list[str] = []
        self.rows: list[tuple[Preset, tk.BooleanVar, list[tuple[Condition, tk.StringVar]]]] = []
        self.scroll = ScrollableFrame(self, height=330 if compact else 150)
        self.scroll.pack(fill="both", expand=True)

    def rebuild(self, filter_sets: str | list[str] | tuple[str, ...] | None = None) -> None:
        if filter_sets is not None:
            if isinstance(filter_sets, str):
                self.filter_sets = [filter_sets] if filter_sets else []
            else:
                self.filter_sets = [str(name) for name in filter_sets if str(name).strip()]
        for child in self.scroll.content.winfo_children():
            child.destroy()
        self.rows.clear()
        available_order = ordered_simple_filter_sets(self.app.settings)
        selected = [name for name in available_order if name in set(self.filter_sets)]
        self.filter_sets = selected
        if not selected:
            ttk.Label(
                self.scroll.content,
                text="上のプルダウンから編集するフィルターセットを選択してください",
            ).pack(anchor="w", padx=4, pady=8)
            return

        multiple_sets = len(selected) > 1
        for set_index, set_name in enumerate(selected):
            presets = simple_presets_for_set(self.app.settings.presets, set_name)
            if multiple_sets:
                header = ttk.Frame(self.scroll.content, padding=(3, 6, 3, 3))
                header.pack(fill="x")
                ttk.Label(header, text=set_name, font=("TkDefaultFont", 10, "bold")).pack(side="left")
                ttk.Label(header, text=f"{len(presets)}項目", foreground="#666666").pack(side="right")
                ttk.Separator(self.scroll.content, orient="horizontal").pack(fill="x", padx=3)
            for index, preset in enumerate(presets):
                row = ttk.Frame(self.scroll.content, padding=(3, 4))
                row.pack(fill="x", expand=True)
                enabled = tk.BooleanVar(
                    value=bool(self.app.settings.simple_preset_enabled.get(preset.preset_id, preset.visible))
                )
                ttk.Checkbutton(
                    row,
                    variable=enabled,
                    command=lambda p=preset, v=enabled: self._toggle_preset(p, v),
                ).grid(row=0, column=0, rowspan=2, sticky="nw", padx=(0, 4))
                display_name = preset.name
                if preset.group_name and simple_filter_set_name(preset) != preset.group_name:
                    display_name = f"{preset.group_name} / {preset.name}"
                ttk.Label(row, text=display_name, font=("TkDefaultFont", 9, "bold")).grid(
                    row=0, column=1, sticky="w"
                )
                values_frame = ttk.Frame(row)
                values_frame.grid(row=1, column=1, sticky="ew", pady=(2, 0))
                numeric_vars: list[tuple[Condition, tk.StringVar]] = []
                fixed_parts: list[str] = []
                numeric_column = 0
                for condition in preset.conditions:
                    info = FIELD_DEFS.get(condition.field, {})
                    label = str(info.get("label", condition.field))
                    if info.get("type") in self.NUMERIC_TYPES:
                        ttk.Label(values_frame, text=f"{label} {condition.op}").grid(
                            row=0, column=numeric_column, sticky="w", padx=(0, 3)
                        )
                        var = tk.StringVar(value=str(condition.value))
                        entry = ttk.Entry(values_frame, textvariable=var, width=8)
                        entry.grid(row=0, column=numeric_column + 1, sticky="w", padx=(0, 9))
                        entry.bind("<Return>", lambda _e, c=condition, v=var: self._commit_one(c, v))
                        entry.bind("<FocusOut>", lambda _e, c=condition, v=var: self._commit_one(c, v))
                        numeric_vars.append((condition, var))
                        numeric_column += 2
                    elif condition.enabled:
                        fixed_parts.append(f"{label} {condition.op} {condition.value}")
                if fixed_parts:
                    ttk.Label(
                        values_frame,
                        text=" / ".join(fixed_parts),
                        foreground="#666666",
                        wraplength=470 if self.compact else 680,
                    ).grid(row=1 if numeric_column else 0, column=0, columnspan=max(1, numeric_column), sticky="w")
                if not numeric_vars and not fixed_parts:
                    ttk.Label(values_frame, text="条件なし", foreground="#777777").grid(row=0, column=0, sticky="w")
                row.columnconfigure(1, weight=1)
                self.rows.append((preset, enabled, numeric_vars))
                if index + 1 < len(presets):
                    ttk.Separator(self.scroll.content, orient="horizontal").pack(fill="x", padx=3)
            if set_index + 1 < len(selected):
                ttk.Separator(self.scroll.content, orient="horizontal").pack(fill="x", padx=3, pady=(5, 1))

    def _toggle_preset(self, preset: Preset, var: tk.BooleanVar) -> None:
        value = bool(var.get())
        self.app.settings.simple_preset_enabled[preset.preset_id] = value
        preset.visible = value
        self.app.filter_settings_changed(self)

    def _commit_one(self, condition: Condition, var: tk.StringVar) -> bool:
        info = FIELD_DEFS.get(condition.field, {})
        text = var.get().strip()
        try:
            if info.get("type") == "int":
                int(text)
            elif info.get("type") == "float":
                float(text)
        except ValueError:
            var.set(str(condition.value))
            self.app.set_quick_status(f"数値が不正です: {info.get('label', condition.field)}")
            return False
        if text != str(condition.value):
            old_value = str(condition.value)
            condition.value = text
            for preset, _enabled, _numeric_vars in self.rows:
                if any(item is condition for item in preset.conditions) and old_value and old_value in preset.name:
                    preset.name = preset.name.replace(old_value, text, 1)
                    break
            self.app.filter_settings_changed(self)
        return True

    def commit_all(self) -> bool:
        ok = True
        for _preset, _enabled, numeric_vars in self.rows:
            for condition, var in numeric_vars:
                ok = self._commit_one(condition, var) and ok
        return ok


class TableSelectionDialog(tk.Toplevel):
    def __init__(self, parent: tk.Misc, app: "MainApp"):
        super().__init__(parent)
        self.app = app
        self.title("対象難易度表")
        self.geometry("620x520")
        self.transient(parent)
        self.grab_set()
        self.result: list[str] | None = None
        self.tables = [table for table in app.tables if not table.error and table.levels]
        outer = ttk.Frame(self, padding=10)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="フィルターを適用する登録済み難易度表を選択してください").pack(anchor="w")
        self.listbox = tk.Listbox(outer, selectmode="multiple", exportselection=False)
        self.listbox.pack(fill="both", expand=True, pady=7)
        selected = set(app.settings.simple_selected_table_ids)
        for index, table in enumerate(self.tables):
            self.listbox.insert("end", f"{table.name}  ({table.chart_count:,}譜面)")
            if table.table_id in selected:
                self.listbox.selection_set(index)
        tools = ttk.Frame(outer)
        tools.pack(fill="x")
        ttk.Button(tools, text="全選択", command=lambda: self.listbox.selection_set(0, "end")).pack(side="left")
        ttk.Button(tools, text="全解除", command=lambda: self.listbox.selection_clear(0, "end")).pack(side="left", padx=5)
        ttk.Button(tools, text="キャンセル", command=self.destroy).pack(side="right")
        ttk.Button(tools, text="決定", command=self.accept).pack(side="right", padx=5)

    def accept(self) -> None:
        self.result = [self.tables[index].table_id for index in self.listbox.curselection()]
        self.destroy()


class FilterSetSelectionDialog(tk.Toplevel):
    def __init__(self, parent: tk.Misc, app: "MainApp"):
        super().__init__(parent)
        self.app = app
        self.title("フィルターセット")
        self.geometry("520x460")
        self.transient(parent)
        self.grab_set()
        self.result: list[str] | None = None
        self.filter_sets = ordered_simple_filter_sets(app.settings)
        outer = ttk.Frame(self, padding=10)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="同時に適用するフィルターセットを選択してください").pack(anchor="w")
        self.listbox = tk.Listbox(outer, selectmode="multiple", exportselection=False)
        self.listbox.pack(fill="both", expand=True, pady=7)
        selected = set(app.settings.simple_selected_filter_sets)
        for index, name in enumerate(self.filter_sets):
            count = len(simple_presets_for_set(app.settings.presets, name))
            self.listbox.insert("end", f"{name}  ({count}項目)")
            if name in selected:
                self.listbox.selection_set(index)
        tools = ttk.Frame(outer)
        tools.pack(fill="x")
        ttk.Button(tools, text="全選択", command=lambda: self.listbox.selection_set(0, "end")).pack(side="left")
        ttk.Button(tools, text="全解除", command=lambda: self.listbox.selection_clear(0, "end")).pack(side="left", padx=5)
        ttk.Button(tools, text="キャンセル", command=self.destroy).pack(side="right")
        ttk.Button(tools, text="決定", command=self.accept).pack(side="right", padx=5)

    def accept(self) -> None:
        self.result = [self.filter_sets[index] for index in self.listbox.curselection()]
        self.destroy()


class QuickPanel(tk.Toplevel):
    """Always-on-top editor for the three conditions useful during play."""

    GROUPS = (
        (
            "最終プレイからの経過日数",
            ("play-stale-7", "play-stale-15", "play-stale-30"),
            "last_play_days",
            "最終プレイから",
            "日以上経過",
        ),
        (
            "最小BP",
            ("minbp-over-30", "minbp-over", "minbp-over-100"),
            "minbp",
            "最小BP",
            "以上",
        ),
        (
            "スコア率",
            ("score-rate-below-70", "score-rate-below", "score-rate-below-90"),
            "score_rate",
            "スコア率",
            "%未満",
        ),
    )

    def __init__(self, app: "MainApp"):
        super().__init__(app.root)
        self.app = app
        self.title("oraja-constellator 小型パネル")
        self.geometry(app.settings.quick_geometry)
        self.minsize(390, 320)
        self.attributes("-topmost", bool(app.settings.quick_topmost))
        self.protocol("WM_DELETE_WINDOW", self.hide)
        self.resizable(True, True)
        self.topmost_var = tk.BooleanVar(value=app.settings.quick_topmost)
        self.rows: list[tuple[Preset, Condition, tk.StringVar]] = []

        outer = ttk.Frame(self, padding=10)
        outer.pack(fill="both", expand=True)

        head = ttk.Frame(outer)
        head.pack(fill="x", pady=(0, 6))
        ttk.Label(head, text="プレイ中に変更する条件", font=("TkDefaultFont", 10, "bold")).pack(side="left")
        ttk.Checkbutton(head, text="最前面", variable=self.topmost_var, command=self.toggle_topmost).pack(
            side="right"
        )

        self.body = ttk.Frame(outer)
        self.body.pack(fill="both", expand=True)

        self.status_var = tk.StringVar(value="")
        ttk.Label(outer, textvariable=self.status_var, foreground="#555555").pack(
            fill="x", pady=(7, 0)
        )

        self.refresh_presets()
        self.withdraw()

    @staticmethod
    def _matches_local_id(preset: Preset, local_id: str) -> bool:
        preset_id = str(preset.preset_id or "")
        return preset_id == local_id or preset_id.endswith(":" + local_id)

    def _find_preset(self, local_id: str) -> Preset | None:
        return next(
            (preset for preset in self.app.settings.presets if self._matches_local_id(preset, local_id)),
            None,
        )

    @staticmethod
    def _set_preset_name(preset: Preset, field_name: str, value: str) -> None:
        if field_name == "last_play_days":
            preset.name = f"最終プレイから{value}日以上経過"
        elif field_name == "minbp":
            preset.name = f"最小BP{value}以上"
        elif field_name == "score_rate":
            preset.name = f"スコア率{value}%未満"

    def refresh_presets(self) -> None:
        for child in self.body.winfo_children():
            child.destroy()
        self.rows.clear()

        for group_name, preset_ids, field_name, prefix, suffix in self.GROUPS:
            frame = ttk.LabelFrame(self.body, text=group_name, padding=(8, 6))
            frame.pack(fill="x", pady=(0, 7))
            displayed = 0
            for local_id in preset_ids:
                preset = self._find_preset(local_id)
                if preset is None:
                    continue
                condition = next((c for c in preset.conditions if c.field == field_name), None)
                if condition is None:
                    continue
                row = ttk.Frame(frame)
                row.pack(fill="x", pady=2)
                ttk.Label(row, text=prefix).pack(side="left")
                var = tk.StringVar(value=str(condition.value))
                entry = ttk.Entry(row, textvariable=var, width=8, justify="right")
                entry.pack(side="left", padx=5)
                ttk.Label(row, text=suffix).pack(side="left")
                entry.bind(
                    "<Return>",
                    lambda _event, p=preset, c=condition, v=var: self._commit_one(p, c, v),
                )
                entry.bind(
                    "<FocusOut>",
                    lambda _event, p=preset, c=condition, v=var: self._commit_one(p, c, v),
                )
                self.rows.append((preset, condition, var))
                displayed += 1
            if displayed == 0:
                ttk.Label(frame, text="設定項目を読み込めませんでした", foreground="#777777").pack(anchor="w")

    def _commit_one(self, preset: Preset, condition: Condition, var: tk.StringVar) -> bool:
        info = FIELD_DEFS.get(condition.field, {})
        text = var.get().strip()
        try:
            if info.get("type") == "int":
                value = int(text)
                if value < 0:
                    raise ValueError
                text = str(value)
            elif info.get("type") == "float":
                value = float(text)
                if value < 0:
                    raise ValueError
                text = f"{value:g}"
        except ValueError:
            var.set(str(condition.value))
            self.status_var.set("0以上の数値を入力してください")
            return False

        var.set(text)
        if text == str(condition.value):
            return True
        condition.value = text
        self._set_preset_name(preset, condition.field, text)
        self.app.save_settings_now()
        self.app.refresh_simple_filter_sets()
        self.status_var.set("条件を反映しています...")
        self.app.request_quick_filter_sync()
        return True

    def commit_all(self) -> bool:
        ok = True
        for preset, condition, var in list(self.rows):
            ok = self._commit_one(preset, condition, var) and ok
        return ok

    def show(self) -> None:
        self.refresh_presets()
        self.deiconify()
        self.lift()
        self.focus_force()

    def hide(self) -> None:
        self.commit_all()
        self.app.settings.quick_geometry = self.geometry()
        self.app.save_settings_now()
        self.withdraw()

    def toggle_topmost(self) -> None:
        value = bool(self.topmost_var.get())
        self.attributes("-topmost", value)
        self.app.settings.quick_topmost = value
        self.app.save_settings_now()

    def apply(self) -> None:
        self.commit_all()

    def generate(self) -> None:
        self.commit_all()


class MakerDiagnosticWindow(tk.Toplevel):
    def __init__(self, app: "MainApp"):
        super().__init__(app.root)
        self.app = app
        self.title("差分制作素材診断")
        self.geometry(app.settings.maker_diagnostic_geometry)
        self.attributes("-topmost", bool(app.settings.maker_diagnostic_topmost))
        self.protocol("WM_DELETE_WINDOW", lambda: app.hide_maker_diagnostic(suppress=True))
        self.current_key = ""
        self.current_folder = ""
        self.title_var = tk.StringVar(value="選択曲なし")
        self.status_var = tk.StringVar(value="")

        outer = ttk.Frame(self, padding=10)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, textvariable=self.title_var, font=("TkDefaultFont", 10, "bold"), wraplength=370).pack(anchor="w")
        ttk.Label(outer, textvariable=self.status_var).pack(anchor="w", pady=(3, 7))
        self.detail = tk.Text(outer, height=13, wrap="word", relief="flat", background=self.cget("background"))
        self.detail.pack(fill="both", expand=True)
        self.detail.configure(state="disabled")
        buttons = ttk.Frame(outer)
        buttons.pack(fill="x", pady=(7, 0))
        ttk.Button(buttons, text="フォルダを開く", command=app.open_current_material_folder).pack(side="left")
        ttk.Button(buttons, text="再解析", command=app.reanalyze_current_material).pack(side="left", padx=5)
        ttk.Button(buttons, text="閉じる", command=lambda: app.hide_maker_diagnostic(suppress=True)).pack(side="right")
        self.withdraw()

    @staticmethod
    def _duration(ms: int) -> str:
        if ms <= 0:
            return "—"
        total = int(round(ms / 1000.0))
        return f"{total // 60}:{total % 60:02d}"

    def set_detail(self, text: str) -> None:
        self.detail.configure(state="normal")
        self.detail.delete("1.0", "end")
        self.detail.insert("1.0", text)
        self.detail.configure(state="disabled")

    def show_material(self, key: str, song: dict[str, Any], material: dict[str, Any]) -> None:
        self.current_key = key
        self.current_folder = str(material.get("folder_path") or "")
        self.title_var.set(str(song.get("title") or "名称不明"))
        ok = bool(material.get("material_ok"))
        self.status_var.set("素材状態：良好" if ok else "素材状態：要確認")
        min_len = self._duration(int(material.get("min_length_ms") or 0))
        max_len = self._duration(int(material.get("max_length_ms") or 0))
        length_text = min_len if min_len == max_len else f"{min_len}～{max_len}"
        lines = [
            f"参考7KEY譜面：{int(material.get('key7_chart_count') or 0):,}",
            f"音源ファイル：{int(material.get('audio_file_count') or 0):,}",
            f"参照済み音源：{int(material.get('referenced_audio_count') or 0):,}",
            f"未使用候補音源：{int(material.get('unused_candidate_count') or 0):,}",
            f"欠損・未定義参照：{int(material.get('missing_reference_count') or 0):,}",
            f"0バイト音源：{int(material.get('zero_byte_audio_count') or 0):,}",
            f"読込エラー：{int(material.get('parse_error_count') or 0):,}",
            f"演奏時間：{length_text}",
            f"BPM変更イベント：{int(material.get('bpm_change_events') or 0):,}",
            f"STOPイベント：{int(material.get('stop_events') or 0):,}",
        ]
        self.set_detail("\n".join(lines))
        self.deiconify()
        self.lift()

    def show_unanalyzed(self, key: str, song: dict[str, Any]) -> None:
        self.current_key = key
        self.current_folder = ""
        self.title_var.set(str(song.get("title") or "名称不明"))
        self.status_var.set("素材状態：未解析")
        self.set_detail("［再解析］を押すと、この楽曲フォルダだけ解析します。")
        self.deiconify()
        self.lift()


class ExistingFolderVisibilityDialog(tk.Toplevel):
    """Manage visibility of pre-existing folder/default.json root entries."""

    def __init__(self, parent: tk.Misc, app: "MainApp"):
        super().__init__(parent)
        self.app = app
        self.title("既存カスタムフォルダーの表示設定")
        self.geometry("760x520")
        self.minsize(620, 380)
        self.transient(parent)
        self.grab_set()

        outer = ttk.Frame(self, padding=10)
        outer.pack(fill="both", expand=True)
        ttk.Label(
            outer,
            text="folder/default.jsonに既にあるカスタムフォルダーを表示・非表示にします。",
        ).pack(anchor="w")
        ttk.Label(
            outer,
            textvariable=app.default_profile_var,
            foreground="#2b579a",
            wraplength=720,
            justify="left",
        ).pack(anchor="w", pady=(3, 7))

        columns = ("visible", "name", "kind")
        self.tree = ttk.Treeview(outer, columns=columns, show="headings", selectmode="extended")
        for key, label, width in (
            ("visible", "表示", 60),
            ("name", "フォルダー名", 470),
            ("kind", "種類", 140),
        ):
            self.tree.heading(key, text=label)
            self.tree.column(key, width=width, stretch=key == "name", anchor="center" if key != "name" else "w")
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<Double-1>", lambda _e: self.toggle_selected())
        self.tree.bind("<space>", lambda _e: self.toggle_selected())
        bind_local_mousewheel(self.tree, self.tree.yview_scroll)

        tools = ttk.Frame(outer)
        tools.pack(fill="x", pady=(7, 0))
        ttk.Button(tools, text="default.jsonから再読込", command=self.reload_catalog).pack(side="left")
        ttk.Button(tools, text="閉じる", command=self.destroy).pack(side="right")
        ttk.Button(tools, text="選択を非表示", command=lambda: self.set_visibility(False)).pack(side="right", padx=5)
        ttk.Button(tools, text="選択を表示", command=lambda: self.set_visibility(True)).pack(side="right")
        ttk.Label(
            outer,
            text="変更はfolder/default.jsonへその場で反映します。beatorajaを起動中の場合は、再起動後に選曲画面へ反映されます。定義は保持されるため後から再表示できます。",
            foreground="#555555",
            wraplength=720,
            justify="left",
        ).pack(anchor="w", pady=(7, 0))
        self.refresh()

    def _folders(self) -> list[DefaultFolderEntry]:
        return active_default_folders(self.app.settings, self.app.env.root if self.app.env else None)

    def refresh(self) -> None:
        selected = set(self.tree.selection())
        self.tree.delete(*self.tree.get_children())
        for entry in self._folders():
            payload = entry.data or {}
            kind = "階層フォルダー" if payload.get("folder") else ("SQLフォルダー" if payload.get("sql") else "その他")
            self.tree.insert(
                "", "end", iid=entry.entry_id,
                values=("ON" if entry.visible else "OFF", entry.name, kind),
            )
        restore = [iid for iid in selected if self.tree.exists(iid)]
        if restore:
            self.tree.selection_set(restore)

    def reload_catalog(self) -> None:
        if not self.app.env:
            messagebox.showerror(APP_NAME, "先にbeatoraja環境を読み込んでください", parent=self)
            return
        try:
            imported = sync_default_folder_catalog(self.app.settings, self.app.env.default_json)
            self.app.save_settings_now()
            self.refresh()
            self.app.status_var.set(f"既存フォルダーを再読込しました（新規{imported}件）")
        except Exception as exc:
            messagebox.showerror(APP_NAME, str(exc), parent=self)

    def set_visibility(self, visible: bool) -> None:
        ids = set(self.tree.selection())
        if not ids:
            return
        count = 0
        for entry in self._folders():
            if entry.entry_id in ids:
                entry.visible = visible
                count += 1
        if count:
            try:
                if not self.app.env:
                    raise ToolError("先にbeatoraja環境を読み込んでください")
                self.app.save_settings_now()
                apply_default_folder_visibility(self.app.env.default_json, self.app.settings)
                self.app.save_settings_now()
                self.refresh()
                message = (
                    f"既存フォルダー{count}件を{'表示' if visible else '非表示'}にし、default.jsonへ反映しました。"
                    "beatorajaを起動中の場合は再起動してください"
                )
                self.app.status_var.set(message)
            except Exception as exc:
                messagebox.showerror(APP_NAME, str(exc), parent=self)

    def toggle_selected(self) -> str:
        ids = set(self.tree.selection())
        if not ids:
            return "break"
        folders = {entry.entry_id: entry for entry in self._folders()}
        target = next((folders[iid] for iid in ids if iid in folders), None)
        self.set_visibility(not bool(target.visible) if target else True)
        return "break"


class MainApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.mode = "simple"
        self.root.title(f"{APP_NAME} v{APP_VERSION}")
        self.analysis_rule_pack_error = ""
        self.analysis_rule_reclass_error = ""
        self.initial_rule_reclass_stats = {"target": 0, "reclassified": 0, "cached": 0, "skipped": 0}
        try:
            reload_active_rule_pack(ANALYSIS_RULE_PACK_DIR)
        except Exception as exc:
            self.analysis_rule_pack_error = str(exc)
            LOGGER.exception("譜面傾向の判定設定を読み込めませんでした")
        if not self.analysis_rule_pack_error:
            try:
                self.initial_rule_reclass_stats = reclassify_chart_analysis(CHART_ANALYSIS_DB, force=False)
            except Exception as exc:
                self.analysis_rule_reclass_error = str(exc)
                LOGGER.exception("保存済み特徴量の再分類に失敗しました")
        self.settings = load_settings(SETTINGS_PATH)
        # 共有版の生成領域と機能範囲を固定し、詳細版の設定を持ち込んでも有効化しない。
        self.settings.root_folder_name = MANAGED_ROOT_NAME
        # Keep the generated root names learned in earlier runs.  Resetting this
        # list on every launch made existing generated roots look like ordinary
        # user folders, so the next apply operation preserved the old copy and
        # added a new one beside it.
        self.settings.managed_folder_names = list(dict.fromkeys(
            [MANAGED_ROOT_NAME] + list(self.settings.managed_folder_names or [])
        ))
        self.settings.instant_density_table_enabled = False
        self.settings.maker_table_enabled = False
        self.settings.random_course_enabled = False
        self.settings.random_course_filter_enabled = True
        self.settings.presets, self.filter_pack_stats = sync_filter_preset_packs(
            self.settings.presets, FILTER_PACK_DIR, preserve_numeric_values=True, pack_only=True
        )
        active_ids = {preset.preset_id for preset in self.settings.presets}
        self.settings.simple_preset_enabled = {
            preset_id: enabled for preset_id, enabled in self.settings.simple_preset_enabled.items()
            if preset_id in active_ids
        }
        for preset in self.settings.presets:
            enabled = self.settings.simple_preset_enabled.setdefault(preset.preset_id, bool(preset.visible))
            preset.visible = bool(enabled)
        save_settings(SETTINGS_PATH, self.settings)
        self.root.geometry(self.settings.simple_geometry)
        self.root.minsize(1000, 680)
        # 共有版では表合成・即席表・難度基準編集を使用しない。
        self.registry_error = ""
        self.instant_profile_error = ""
        self.difficulty_registry = {"tables": [], "sources": []}
        self.instant_density_profile = {"bands": {}}
        self.env: EnvironmentPaths | None = None
        self.tables: list[TableInfo] = []
        self.table_by_id: dict[str, TableInfo] = {}
        self.worker_queue: queue.Queue[tuple[Callable[[Any, Exception | None], None], Any, Exception | None]] = queue.Queue()
        self.progress_queue: queue.Queue[ProgressUpdate] = queue.Queue(maxsize=1)
        self.worker_thread: threading.Thread | None = None
        self.progress_active = False
        self.progress_started_at = 0.0
        self.progress_last_update_at = 0.0
        self.progress_last_change_at = 0.0
        self.progress_last_overall = 0.0
        self.progress_overall_total = 1.0
        self.progress_samples: list[tuple[float, float]] = []
        self.progress_task_name = ""
        self.busy = False
        self.analysis_running = False
        self.analysis_cancel_event = threading.Event()
        self.pending_apply_after_analysis = False
        self.analysis_cancel_button: ttk.Button | None = None
        self.analysis_start_button: ttk.Button | None = None
        self.analysis_commit_button: ttk.Button | None = None
        self.player_combo: ttk.Combobox | None = None
        self.simple_table_sort_column = "name"
        self.simple_table_sort_reverse = False
        self.simple_table_menu: tk.Menu | None = None
        self.simple_filter_set_menu: tk.Menu | None = None
        self.status_var = tk.StringVar(value="beatoraja環境を読み込んでください")
        self.root_var = tk.StringVar(value=self.settings.rian_root)
        self.player_var = tk.StringVar(value=self.settings.player_name or "player1")
        self.analysis_target_var = tk.StringVar(value="解析対象: 対象難易度表を選択してください")
        self.pending_analysis_var = tk.StringVar(value="未追記の解析結果: なし")
        self.env_text_var = tk.StringVar(value="未読込")
        self.default_profile_var = tk.StringVar(value="現在の環境を読み込むと、環境別の表示設定へ切り替わります")
        self.preview_var = tk.StringVar(value="未計算")
        self.progress_state_var = tk.StringVar(value="待機")
        self.progress_stage_var = tk.StringVar(value="処理は実行されていません")
        self.progress_detail_var = tk.StringVar(value="")
        self.progress_time_var = tk.StringVar(value="経過 --:-- / 残り --:--")
        self.progress_percent_var = tk.StringVar(value="0.0%")
        self.progress_value_var = tk.DoubleVar(value=0.0)
        self.progress_bar: ttk.Progressbar | None = None
        self.instant_enabled_var = tk.BooleanVar(value=self.settings.instant_density_table_enabled)
        self.maker_enabled_var = tk.BooleanVar(value=self.settings.maker_table_enabled)
        self.random_course_enabled_var = tk.BooleanVar(value=self.settings.random_course_enabled)
        self.random_course_filter_enabled_var = tk.BooleanVar(value=self.settings.random_course_filter_enabled)
        self.random_course_stages_var = tk.IntVar(value=self.settings.random_course_stages)
        self.random_course_distinct_var = tk.BooleanVar(value=self.settings.random_course_distinct)
        self.random_course_folder_name_var = tk.StringVar(value=self.settings.random_course_folder_name)
        self.random_course_name_var = tk.StringVar(value=self.settings.random_course_name)
        self.random_course_filter_name_var = tk.StringVar(value=self.settings.random_course_filter_name)
        self.history_path_var = tk.StringVar(value=self.settings.current_song_history_path)
        profile_text = ""
        self.instant_profile_var = tk.StringVar(value=profile_text)
        self.log_text: tk.Text | None = None
        self.table_tree: ttk.Treeview | None = None
        self.combination_tree: ttk.Treeview | None = None
        self.default_folder_tree: ttk.Treeview | None = None
        self.scale_tree: ttk.Treeview | None = None
        self.scale_detail: tk.Text | None = None
        self.preset_tree: ttk.Treeview | None = None
        self.preset_summary_var = tk.StringVar(value="")
        self.simple_table_tree: ttk.Treeview | None = None
        self.simple_filter_set_tree: ttk.Treeview | None = None
        self.simple_filter_set_combo: ttk.Combobox | None = None
        self.simple_filter_editor: FilterListEditor | None = None
        self.simple_filter_set_var = tk.StringVar(value=self.settings.simple_filter_set)
        self.simple_table_summary_var = tk.StringVar(value="対象表: 未読込")
        self.simple_filter_set_summary_var = tk.StringVar(value="フィルターセット: 未選択")
        self.filter_pack_summary_var = tk.StringVar(value=self._filter_pack_summary())
        self.analysis_rule_pack_summary_var = tk.StringVar(value=self._analysis_rule_pack_summary())
        self.update_source_error = ""
        try:
            self.update_source = load_update_source(UPDATE_SOURCE_PATH)
        except Exception as exc:
            self.update_source = {"manifest_url": "", "repository_url": "", "timeout_seconds": 15}
            self.update_source_error = str(exc)
        self.update_manifest: dict[str, Any] = {}
        self.update_summary: dict[str, Any] = {}
        self.update_checking = False
        self.update_result_queue: queue.Queue[Callable[[], None]] = queue.Queue()
        self.update_status_var = tk.StringVar(value=self._initial_update_status())
        self.update_notice_var = tk.StringVar(value="")
        self.update_check_on_startup_var = tk.BooleanVar(value=self.settings.update_check_on_startup)
        self.update_check_button: ttk.Button | None = None
        self.update_settings_button: ttk.Button | None = None
        self.update_app_button: ttk.Button | None = None
        self.quick_panel = QuickPanel(self)
        self.maker_diagnostic = None
        self.last_history_signature: tuple[str, int] | None = None
        self.current_material_sha = ""
        self.current_material_md5 = ""
        self.current_material_key = ""
        self.diagnostic_suppressed_key = ""
        self.build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.report_callback_exception = self.report_callback_exception  # type: ignore[method-assign]
        self.root.after(100, self.poll_worker)
        self.root.after(250, self.poll_progress)
        self.root.after(120, self.poll_update_results)
        self.try_auto_load()
        self.root.after(1500, self.maybe_check_updates_on_startup)

    def _initial_update_status(self) -> str:
        if self.update_source_error:
            return "更新先設定を読み込めません"
        if not str(self.update_source.get("manifest_url") or "").strip():
            return "更新先は未設定です"
        return "更新情報: 未確認"

    def _current_filter_version(self) -> str:
        infos = (getattr(self, "filter_pack_stats", {}) or {}).get("pack_infos") or []
        versions = [str(info.get("version") or "0") for info in infos if isinstance(info, dict)]
        if not versions:
            return "0"
        return max(versions, key=version_key)

    def _current_tendency_version(self) -> str:
        try:
            return str(get_active_rule_pack_info().get("version") or "0")
        except Exception:
            return "0"

    def _update_user_agent(self) -> str:
        return f"{APP_NAME}/{APP_VERSION}"

    def _set_update_buttons(self) -> None:
        checking_state = "disabled" if self.update_checking else "normal"
        if self.update_check_button is not None:
            self.update_check_button.configure(state=checking_state)
        summary = self.update_summary or {}
        settings_available = bool(summary.get("filter_available") or summary.get("tendency_available"))
        app_available = bool(summary.get("app_available"))
        if self.update_settings_button is not None:
            self.update_settings_button.configure(
                state="normal" if settings_available and not self.update_checking else "disabled"
            )
        if self.update_app_button is not None:
            self.update_app_button.configure(
                state="normal" if app_available and not self.update_checking else "disabled"
            )

    def save_update_preference(self) -> None:
        self.settings.update_check_on_startup = bool(self.update_check_on_startup_var.get())
        self.save_settings_now()

    def maybe_check_updates_on_startup(self) -> None:
        if not self.update_check_on_startup_var.get():
            return
        if not str(self.update_source.get("manifest_url") or "").strip():
            return
        now = int(time.time())
        if now - int(self.settings.last_update_check_ts or 0) < 24 * 60 * 60:
            return
        self.check_updates(manual=False)

    def check_updates(self, manual: bool = True) -> None:
        if self.update_checking:
            return
        manifest_url = str(self.update_source.get("manifest_url") or "").strip()
        if not manifest_url:
            message = "更新先が設定されていません。"
            self.update_status_var.set(message)
            if manual:
                messagebox.showinfo(APP_NAME, message, parent=self.root)
            return
        self.update_checking = True
        self.update_status_var.set("更新情報を確認しています…")
        self._set_update_buttons()
        timeout = int(self.update_source.get("timeout_seconds") or 15)

        def worker() -> None:
            try:
                manifest = fetch_manifest(
                    manifest_url,
                    timeout=timeout,
                    user_agent=self._update_user_agent(),
                )
                result: tuple[dict[str, Any] | None, Exception | None] = (manifest, None)
            except Exception as exc:
                result = (None, exc)
            self.update_result_queue.put(
                lambda manifest=result[0], error=result[1]: self._finish_update_check(manifest, error, manual)
            )

        threading.Thread(target=worker, name="update-check", daemon=True).start()

    def _finish_update_check(
        self,
        manifest: dict[str, Any] | None,
        error: Exception | None,
        manual: bool,
    ) -> None:
        self.update_checking = False
        if error is not None or manifest is None:
            self.update_status_var.set("更新情報を確認できませんでした")
            self.update_notice_var.set("")
            self.append_log(f"更新確認失敗: {error}")
            self._set_update_buttons()
            if manual:
                messagebox.showerror(APP_NAME, f"更新情報を確認できませんでした。\n\n{error}", parent=self.root)
            return
        self.update_manifest = manifest
        self.update_summary = manifest_update_summary(
            manifest,
            app_version=APP_VERSION,
            filter_version=self._current_filter_version(),
            tendency_version=self._current_tendency_version(),
        )
        self.settings.last_update_check_ts = int(time.time())
        self.save_settings_now()
        summary = self.update_summary
        labels: list[str] = []
        if summary.get("app_available"):
            labels.append(f"ツール {summary['app_current']} → {summary['app_remote']}")
        if summary.get("filter_available"):
            labels.append(f"フィルター設定 {summary['filter_current']} → {summary['filter_remote']}")
        if summary.get("tendency_available"):
            labels.append(f"譜面傾向判定 {summary['tendency_current']} → {summary['tendency_remote']}")
        if labels:
            text = "更新あり: " + " / ".join(labels)
            self.update_status_var.set(text)
            self.update_notice_var.set("新しい更新があります。「設定・管理」タブから確認できます。")
        else:
            self.update_status_var.set("すべて最新です")
            self.update_notice_var.set("")
        self._set_update_buttons()
        if manual:
            messagebox.showinfo(APP_NAME, self.update_status_var.get(), parent=self.root)

    def update_settings_files(self) -> None:
        if not self.update_manifest:
            self.check_updates(manual=True)
            return
        summary = self.update_summary or {}
        targets: list[tuple[str, dict[str, Any]]] = []
        if summary.get("filter_available"):
            entry = self.update_manifest.get("filter_settings")
            if isinstance(entry, dict):
                targets.append(("filter", entry))
        if summary.get("tendency_available"):
            entry = self.update_manifest.get("chart_tendency")
            if isinstance(entry, dict):
                targets.append(("tendency", entry))
        if not targets:
            messagebox.showinfo(APP_NAME, "更新できる設定ファイルはありません。", parent=self.root)
            return
        for kind, entry in targets:
            minimum = str(entry.get("min_app_version") or "").strip()
            if minimum and is_newer(minimum, APP_VERSION):
                messagebox.showerror(
                    APP_NAME,
                    f"この設定更新にはツールv{minimum}以上が必要です。先にツール本体を更新してください。",
                    parent=self.root,
                )
                return
            if kind == "tendency":
                required_schema = int(entry.get("feature_schema_version") or FEATURE_SCHEMA_VERSION)
                if required_schema != FEATURE_SCHEMA_VERSION:
                    messagebox.showerror(
                        APP_NAME,
                        f"この譜面傾向判定には特徴量形式v{required_schema}が必要です。"
                        f"現在のツールはv{FEATURE_SCHEMA_VERSION}です。先にツール本体を更新してください。",
                        parent=self.root,
                    )
                    return
        self.update_checking = True
        self.update_status_var.set("設定ファイルを更新しています…")
        self._set_update_buttons()
        timeout = int(self.update_source.get("timeout_seconds") or 15)

        def worker() -> None:
            changed: list[str] = []
            try:
                with tempfile.TemporaryDirectory(prefix="bmsff_settings_update_") as td:
                    temp_dir = Path(td)
                    for kind, entry in targets:
                        filename = str(entry.get("filename") or (
                            "official_standard_filters.json" if kind == "filter" else "official_attribute_rules.json"
                        )).strip()
                        downloaded = download_verified(
                            entry,
                            temp_dir / filename,
                            timeout=timeout,
                            user_agent=self._update_user_agent(),
                        )
                        if kind == "filter":
                            presets, messages, files, infos = load_filter_preset_packs(temp_dir)
                            if messages or files != 1 or not presets or len(infos) != 1:
                                raise UpdateError("フィルター設定として読み込めません: " + " / ".join(messages))
                            atomic_replace_with_backup(
                                downloaded,
                                FILTER_PACK_DIR / filename,
                                FILTER_PACK_DIR / "backup",
                            )
                            changed.append("フィルター設定")
                        else:
                            load_rule_pack_file(downloaded)
                            atomic_replace_with_backup(
                                downloaded,
                                ANALYSIS_RULE_PACK_DIR / filename,
                                ANALYSIS_RULE_PACK_DIR / "backup",
                            )
                            changed.append("譜面傾向判定")
                result: tuple[list[str] | None, Exception | None] = (changed, None)
            except Exception as exc:
                result = (None, exc)
            self.update_result_queue.put(
                lambda changed=result[0], error=result[1]: self._finish_settings_update(changed, error)
            )

        threading.Thread(target=worker, name="settings-update", daemon=True).start()

    def _finish_settings_update(self, changed: list[str] | None, error: Exception | None) -> None:
        self.update_checking = False
        if error is not None or changed is None:
            self.update_status_var.set("設定ファイルの更新に失敗しました")
            self.append_log(f"設定更新失敗: {error}")
            self._set_update_buttons()
            messagebox.showerror(APP_NAME, f"設定ファイルを更新できませんでした。\n\n{error}", parent=self.root)
            return
        try:
            if "フィルター設定" in changed:
                self.reload_filter_packs()
            if "譜面傾向判定" in changed:
                self.reload_analysis_rule_pack(show_dialog=False)
            self.update_status_var.set(" / ".join(changed) + "を更新しました")
            self.append_log(self.update_status_var.get())
            messagebox.showinfo(APP_NAME, self.update_status_var.get(), parent=self.root)
            self.check_updates(manual=False)
        except Exception as exc:
            self.update_status_var.set("更新後の再読込に失敗しました")
            self._set_update_buttons()
            messagebox.showerror(APP_NAME, f"更新ファイルの再読込に失敗しました。\n\n{exc}", parent=self.root)

    def update_tool(self) -> None:
        summary = self.update_summary or {}
        entry = self.update_manifest.get("app") if isinstance(self.update_manifest, dict) else None
        if not summary.get("app_available") or not isinstance(entry, dict):
            messagebox.showinfo(APP_NAME, "ツール本体は最新です。", parent=self.root)
            return
        remote = str(entry.get("version") or "")
        if not messagebox.askyesno(
            APP_NAME,
            f"ツールをv{APP_VERSION}からv{remote}へ更新します。\n"
            "設定・解析DBは残したまま、ツール本体を置き換えて再起動します。\n\n続行しますか？",
            parent=self.root,
        ):
            return
        self.update_checking = True
        self.update_status_var.set("ツール本体をダウンロードしています…")
        self._set_update_buttons()
        timeout = int(self.update_source.get("timeout_seconds") or 15)
        target = UPDATE_DOWNLOAD_DIR / f"oraja-constellator_v{remote}.zip"

        def worker() -> None:
            try:
                downloaded = download_verified(
                    entry,
                    target,
                    timeout=timeout,
                    user_agent=self._update_user_agent(),
                )
                result: tuple[Path | None, Exception | None] = (downloaded, None)
            except Exception as exc:
                result = (None, exc)
            self.update_result_queue.put(
                lambda zip_path=result[0], error=result[1]: self._launch_tool_updater(zip_path, error)
            )

        threading.Thread(target=worker, name="app-update-download", daemon=True).start()

    def _launch_tool_updater(self, zip_path: Path | None, error: Exception | None) -> None:
        self.update_checking = False
        if error is not None or zip_path is None:
            self.update_status_var.set("ツール本体のダウンロードに失敗しました")
            self._set_update_buttons()
            messagebox.showerror(APP_NAME, f"ツール本体を更新できませんでした。\n\n{error}", parent=self.root)
            return
        helper = PYTHON_DIR / "update_helper.py"
        if not helper.is_file():
            self.update_status_var.set("更新用プログラムが見つかりません")
            self._set_update_buttons()
            messagebox.showerror(APP_NAME, f"更新用プログラムが見つかりません。\n{helper}", parent=self.root)
            return
        try:
            creationflags = 0
            if os.name == "nt":
                creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(
                    subprocess, "DETACHED_PROCESS", 0
                )
            subprocess.Popen(
                [
                    sys.executable,
                    str(helper),
                    "--root",
                    str(APP_DIR),
                    "--zip",
                    str(zip_path),
                    "--pid",
                    str(os.getpid()),
                ],
                cwd=APP_DIR,
                close_fds=True,
                creationflags=creationflags,
            )
            self.update_status_var.set("ツールを終了して更新します…")
            self.on_close()
        except Exception as exc:
            self.update_status_var.set("更新用プログラムを起動できませんでした")
            self._set_update_buttons()
            messagebox.showerror(APP_NAME, f"更新処理を開始できませんでした。\n\n{exc}", parent=self.root)

    def build_ui(self) -> None:
        self.build_simple_ui()

    def build_detailed_ui(self) -> None:
        outer = ttk.Frame(self.root, padding=8)
        outer.pack(fill="both", expand=True)
        notebook = ttk.Notebook(outer)
        notebook.pack(fill="both", expand=True)

        self.env_tab = ttk.Frame(notebook, padding=10)
        self.table_tab = ttk.Frame(notebook, padding=10)
        self.structure_tab = ttk.Frame(notebook, padding=10)
        self.scale_tab = ttk.Frame(notebook, padding=10)
        self.preset_tab = ttk.Frame(notebook, padding=10)
        self.generate_tab = ttk.Frame(notebook, padding=10)
        notebook.add(self.env_tab, text="環境")
        notebook.add(self.table_tab, text="難易度表")
        notebook.add(self.structure_tab, text="フォルダ構成")
        notebook.add(self.scale_tab, text="難度基準")
        notebook.add(self.preset_tab, text="フィルター")
        notebook.add(self.generate_tab, text="生成・更新")

        self.build_env_tab()
        self.build_table_tab()
        self.build_structure_tab()
        self.build_scale_tab()
        self.build_preset_tab()
        self.build_generate_tab()

        status = ttk.Frame(outer)
        status.pack(fill="x", pady=(6, 0))
        ttk.Label(status, textvariable=self.status_var).pack(side="left", fill="x", expand=True)
        ttk.Button(status, text="ログを開く", command=lambda: open_path(LOG_PATH)).pack(side="right")
        ttk.Button(status, text="シンプルモードで再起動", command=self.launch_simple_mode).pack(side="right", padx=6)

    def build_simple_ui(self) -> None:
        outer = ttk.Frame(self.root, padding=8)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(0, weight=1)

        notebook = ttk.Notebook(outer)
        notebook.grid(row=0, column=0, sticky="nsew")
        main_tab = ttk.Frame(notebook, padding=8)
        settings_tab = ttk.Frame(notebook, padding=8)
        notebook.add(main_tab, text="カスタムフォルダー設定")
        notebook.add(settings_tab, text="設定・管理")
        self.simple_notebook = notebook
        self.simple_main_tab = main_tab
        self.simple_settings_tab = settings_tab

        # --- Main tab -----------------------------------------------------
        main_tab.columnconfigure(0, weight=1, uniform="main")
        main_tab.columnconfigure(1, weight=1, uniform="main")
        main_tab.rowconfigure(1, weight=0)
        main_tab.rowconfigure(2, weight=1)

        guide = (
            "初回利用時は「設定・管理」タブで本体フォルダーとプレイヤーを指定してください。"
            "楽曲や差分を追加した後は、対象難易度表を確認してから差分解析を行います。"
        )
        guide_frame = ttk.Frame(main_tab)
        guide_frame.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 7))
        guide_frame.columnconfigure(0, weight=1)
        ttk.Label(guide_frame, text=guide, wraplength=1050, justify="left").grid(row=0, column=0, sticky="ew")
        ttk.Label(
            guide_frame,
            textvariable=self.update_notice_var,
            foreground="#a05a00",
        ).grid(row=1, column=0, sticky="w", pady=(2, 0))

        target = ttk.LabelFrame(main_tab, text="対象難易度表", padding=7)
        target.grid(row=1, column=0, rowspan=2, sticky="nsew", padx=(0, 4))
        target.columnconfigure(0, weight=1)
        target.rowconfigure(0, weight=1)
        columns = ("selected", "charts", "name")
        tree = ttk.Treeview(target, columns=columns, show="headings", height=15, selectmode="extended")
        self.simple_table_tree = tree
        for key, label, width in (
            ("selected", "使用", 52),
            ("charts", "譜面", 80),
            ("name", "難易度表", 390),
        ):
            tree.heading(key, text=label, command=lambda k=key: self.sort_simple_tables(k))
            tree.column(key, width=width, stretch=key == "name", anchor="center" if key != "name" else "w")
        tree.grid(row=0, column=0, sticky="nsew")
        table_scroll = ttk.Scrollbar(target, orient="vertical", command=tree.yview)
        table_scroll.grid(row=0, column=1, sticky="ns")
        tree.configure(yscrollcommand=table_scroll.set)
        tree.bind("<Double-1>", lambda _e: self.toggle_simple_table_selection())
        tree.bind("<space>", lambda _e: self.toggle_simple_table_selection())
        tree.bind("<Button-3>", self.show_simple_table_menu)
        bind_local_mousewheel(tree, tree.yview_scroll)
        self.simple_table_menu = tk.Menu(tree, tearoff=False)
        self.simple_table_menu.add_command(label="ONにする", command=lambda: self.set_selected_simple_tables(True))
        self.simple_table_menu.add_command(label="OFFにする", command=lambda: self.set_selected_simple_tables(False))
        self.simple_table_menu.add_command(label="ON/OFFを切り替える", command=self.toggle_simple_table_selection)
        self.simple_table_menu.add_separator()
        self.simple_table_menu.add_command(label="すべてON", command=lambda: self.set_all_simple_tables(True))
        self.simple_table_menu.add_command(label="すべてOFF", command=lambda: self.set_all_simple_tables(False))
        table_tools = ttk.Frame(target)
        table_tools.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(5, 0))
        ttk.Button(table_tools, text="すべてON", command=lambda: self.set_all_simple_tables(True)).pack(side="left")
        ttk.Button(table_tools, text="すべてOFF", command=lambda: self.set_all_simple_tables(False)).pack(side="left", padx=5)
        ttk.Label(table_tools, text="右クリックで選択項目をON/OFF", foreground="#666666").pack(side="left", padx=(8, 0))
        ttk.Label(table_tools, textvariable=self.simple_table_summary_var).pack(side="right")

        target_sets = ttk.LabelFrame(main_tab, text="対象フィルターセット", padding=7)
        target_sets.grid(row=1, column=1, sticky="ew", padx=(4, 0), pady=(0, 4))
        target_sets.columnconfigure(0, weight=1)
        set_columns = ("selected", "count", "name")
        set_tree = ttk.Treeview(
            target_sets, columns=set_columns, show="headings", height=6, selectmode="extended"
        )
        self.simple_filter_set_tree = set_tree
        for key, label, width in (
            ("selected", "使用", 52),
            ("count", "項目", 58),
            ("name", "フィルターセット", 330),
        ):
            set_tree.heading(key, text=label)
            set_tree.column(key, width=width, stretch=key == "name", anchor="center" if key != "name" else "w")
        set_tree.grid(row=0, column=0, sticky="ew")
        set_scroll = ttk.Scrollbar(target_sets, orient="vertical", command=set_tree.yview)
        set_scroll.grid(row=0, column=1, sticky="ns")
        set_tree.configure(yscrollcommand=set_scroll.set)
        set_tree.bind("<Double-1>", lambda _e: self.toggle_simple_filter_set_selection())
        set_tree.bind("<space>", lambda _e: self.toggle_simple_filter_set_selection())
        set_tree.bind("<Button-3>", self.show_simple_filter_set_menu)
        bind_local_mousewheel(set_tree, set_tree.yview_scroll)
        self.simple_filter_set_menu = tk.Menu(set_tree, tearoff=False)
        self.simple_filter_set_menu.add_command(label="ONにする", command=lambda: self.set_selected_simple_filter_sets(True))
        self.simple_filter_set_menu.add_command(label="OFFにする", command=lambda: self.set_selected_simple_filter_sets(False))
        self.simple_filter_set_menu.add_command(label="ON/OFFを切り替える", command=self.toggle_simple_filter_set_selection)
        self.simple_filter_set_menu.add_separator()
        self.simple_filter_set_menu.add_command(label="すべてON", command=lambda: self.set_all_simple_filter_sets(True))
        self.simple_filter_set_menu.add_command(label="すべてOFF", command=lambda: self.set_all_simple_filter_sets(False))
        set_tools = ttk.Frame(target_sets)
        set_tools.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(5, 0))
        ttk.Button(set_tools, text="↑", width=4, command=lambda: self.move_simple_filter_set(-1)).pack(side="left")
        ttk.Button(set_tools, text="↓", width=4, command=lambda: self.move_simple_filter_set(1)).pack(side="left", padx=(4, 8))
        ttk.Button(set_tools, text="すべてON", command=lambda: self.set_all_simple_filter_sets(True)).pack(side="left")
        ttk.Button(set_tools, text="すべてOFF", command=lambda: self.set_all_simple_filter_sets(False)).pack(side="left", padx=5)
        ttk.Label(set_tools, text="右クリックで選択項目をON/OFF", foreground="#666666").pack(side="left", padx=(8, 0))
        ttk.Label(set_tools, textvariable=self.simple_filter_set_summary_var).pack(side="right")

        details = ttk.LabelFrame(main_tab, text="フィルター詳細設定", padding=7)
        details.grid(row=2, column=1, sticky="nsew", padx=(4, 0), pady=(4, 0))
        details.rowconfigure(1, weight=1)
        details.columnconfigure(0, weight=1)
        detail_head = ttk.Frame(details)
        detail_head.grid(row=0, column=0, sticky="ew", pady=(0, 5))
        ttk.Label(detail_head, text="編集するフィルターセット").pack(side="left")
        self.simple_filter_set_combo = ttk.Combobox(
            detail_head, textvariable=self.simple_filter_set_var, state="readonly", width=30
        )
        self.simple_filter_set_combo.pack(side="left", fill="x", expand=True, padx=(7, 0))
        self.simple_filter_set_combo.bind("<<ComboboxSelected>>", lambda _e: self.set_simple_filter_edit_set())
        self.simple_filter_editor = FilterListEditor(details, self, compact=False)
        self.simple_filter_editor.grid(row=1, column=0, sticky="nsew")
        apply_row = ttk.Frame(details)
        apply_row.grid(row=2, column=0, sticky="ew", pady=(7, 0))
        ttk.Label(
            apply_row,
            text="現在の難易度表・フィルター設定をbeatorajaのカスタムフォルダーへ反映します",
            foreground="#666666",
        ).pack(side="left", fill="x", expand=True)
        ttk.Button(apply_row, text="カスタムフォルダーへ反映", command=self.request_apply_changes).pack(side="right")

        # --- Settings / management tab -----------------------------------
        settings_tab.columnconfigure(0, weight=1)
        settings_tab.columnconfigure(1, weight=1)
        settings_tab.rowconfigure(1, weight=1)

        env_frame = ttk.LabelFrame(settings_tab, text="本体フォルダーと差分解析", padding=9)
        env_frame.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        env_frame.columnconfigure(1, weight=1)
        ttk.Label(
            env_frame,
            text=(
                "解析対象はメインタブでONにした難易度表の差分だけです。"
                "差分数が多く時間がかかる場合は、対象難易度表を減らしてから解析してください。"
                "解析中も難易度表やフィルターを編集できます。"
            ),
            wraplength=1040,
            justify="left",
        ).grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 7))
        ttk.Label(env_frame, text="本体フォルダー").grid(row=1, column=0, sticky="w")
        ttk.Entry(env_frame, textvariable=self.root_var).grid(row=1, column=1, sticky="ew", padx=6)
        ttk.Button(env_frame, text="参照", command=self.browse_root).grid(row=1, column=2, padx=2)
        ttk.Button(env_frame, text="読込・再読込", command=self.load_environment).grid(row=1, column=3, padx=2)
        ttk.Label(env_frame, text="プレイヤー").grid(row=2, column=0, sticky="w", pady=(5, 0))
        self.player_combo = ttk.Combobox(env_frame, textvariable=self.player_var, state="readonly", width=24)
        self.player_combo.grid(row=2, column=1, sticky="w", padx=6, pady=(5, 0))
        self.player_combo.configure(values=(self.player_var.get() or "player1",))
        ttk.Label(env_frame, text="通常はplayer1です", foreground="#666666").grid(
            row=2, column=2, columnspan=2, sticky="w", padx=2, pady=(5, 0)
        )
        ttk.Label(env_frame, textvariable=self.env_text_var, justify="left", foreground="#555555").grid(
            row=3, column=0, columnspan=4, sticky="w", pady=(6, 3)
        )
        ttk.Label(env_frame, textvariable=self.analysis_target_var, foreground="#444444").grid(
            row=4, column=0, columnspan=4, sticky="w", pady=(2, 0)
        )
        ttk.Label(env_frame, textvariable=self.pending_analysis_var, foreground="#444444").grid(
            row=5, column=0, columnspan=4, sticky="w", pady=(2, 5)
        )
        analysis_actions = ttk.Frame(env_frame)
        analysis_actions.grid(row=6, column=0, columnspan=4, sticky="ew")
        self.analysis_start_button = ttk.Button(
            analysis_actions, text="差分を解析", command=lambda: self.start_analysis(force=False)
        )
        self.analysis_start_button.pack(side="left")
        self.analysis_cancel_button = ttk.Button(
            analysis_actions, text="解析を中止", command=self.cancel_analysis, state="disabled"
        )
        self.analysis_cancel_button.pack(side="left", padx=6)
        self.analysis_commit_button = ttk.Button(
            analysis_actions, text="DBへ追記", command=self.commit_pending_analysis
        )
        self.analysis_commit_button.pack(side="left")
        ttk.Button(
            analysis_actions,
            text="カスタムフォルダーへ反映",
            command=self.request_apply_changes,
        ).pack(side="left", padx=(6, 0))
        ttk.Label(analysis_actions, textvariable=self.status_var).pack(side="left", fill="x", expand=True, padx=(8, 0))

        general = ttk.LabelFrame(settings_tab, text="表示・カスタムフォルダー", padding=9)
        general.grid(row=1, column=0, sticky="nsew", padx=(0, 4))
        ttk.Button(general, text="小型パネルの表示", command=self.quick_panel.show).pack(fill="x", pady=(0, 6))
        ttk.Button(general, text="カスタムフォルダー編集", command=self.show_existing_folder_visibility).pack(fill="x")

        config_files = ttk.LabelFrame(settings_tab, text="設定ファイル", padding=9)
        config_files.grid(row=1, column=1, sticky="nsew", padx=(4, 0))
        filter_group = ttk.LabelFrame(config_files, text="フィルター設定", padding=7)
        filter_group.pack(fill="x", pady=(0, 7))
        ttk.Label(
            filter_group,
            text="使用するフィルターの定義ファイルです。通常は変更不要です。",
            foreground="#666666",
        ).pack(anchor="w", pady=(0, 5))
        filter_buttons = ttk.Frame(filter_group)
        filter_buttons.pack(fill="x")
        ttk.Button(filter_buttons, text="設定を再読込", command=self.reload_filter_packs).pack(side="left")
        ttk.Button(
            filter_buttons, text="初期値へ戻す", command=lambda: self.reload_filter_packs(reset_values=True)
        ).pack(side="left", padx=5)
        ttk.Button(filter_buttons, text="フォルダーを開く", command=self.open_filter_pack_dir).pack(side="left")

        tendency_group = ttk.LabelFrame(config_files, text="譜面傾向の判定設定", padding=7)
        tendency_group.pack(fill="x")
        ttk.Label(
            tendency_group,
            text="16分乱打・腕ガチ・指ガチ・ディレイの判定設定です。通常は変更不要です。",
            foreground="#666666",
        ).pack(anchor="w", pady=(0, 5))
        tendency_buttons = ttk.Frame(tendency_group)
        tendency_buttons.pack(fill="x")
        ttk.Button(tendency_buttons, text="設定を再読込", command=self.reload_analysis_rule_pack).pack(side="left")
        ttk.Button(tendency_buttons, text="フォルダーを開く", command=self.open_analysis_rule_pack_dir).pack(side="left", padx=5)

        update_frame = ttk.LabelFrame(settings_tab, text="更新", padding=9)
        update_frame.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        update_frame.columnconfigure(0, weight=1)
        ttk.Label(update_frame, textvariable=self.update_status_var).grid(row=0, column=0, columnspan=4, sticky="w")
        update_actions = ttk.Frame(update_frame)
        update_actions.grid(row=1, column=0, columnspan=4, sticky="ew", pady=(6, 0))
        self.update_check_button = ttk.Button(update_actions, text="更新を確認", command=lambda: self.check_updates(manual=True))
        self.update_check_button.pack(side="left")
        self.update_settings_button = ttk.Button(
            update_actions, text="設定ファイルを更新", command=self.update_settings_files, state="disabled"
        )
        self.update_settings_button.pack(side="left", padx=6)
        self.update_app_button = ttk.Button(
            update_actions, text="ツールを更新", command=self.update_tool, state="disabled"
        )
        self.update_app_button.pack(side="left")
        ttk.Checkbutton(
            update_actions,
            text="起動時に更新を確認する",
            variable=self.update_check_on_startup_var,
            command=self.save_update_preference,
        ).pack(side="right")
        self._set_update_buttons()

        # --- Shared progress / log area ----------------------------------
        progress_frame = ttk.LabelFrame(outer, text="処理状況", padding=6)
        progress_frame.grid(row=1, column=0, sticky="ew", pady=(7, 5))
        header = ttk.Frame(progress_frame)
        header.pack(fill="x")
        ttk.Label(header, textvariable=self.progress_state_var, width=24).pack(side="left")
        ttk.Label(header, textvariable=self.progress_stage_var).pack(side="left", fill="x", expand=True)
        ttk.Label(header, textvariable=self.progress_percent_var, width=9, anchor="e").pack(side="right")
        self.progress_bar = ttk.Progressbar(
            progress_frame, variable=self.progress_value_var, maximum=100.0, mode="determinate"
        )
        self.progress_bar.pack(fill="x", pady=(3, 1))
        detail_row = ttk.Frame(progress_frame)
        detail_row.pack(fill="x")
        ttk.Label(detail_row, textvariable=self.progress_detail_var, foreground="#555555").pack(
            side="left", fill="x", expand=True
        )
        ttk.Label(detail_row, textvariable=self.progress_time_var).pack(side="right", padx=(12, 0))

        log_frame = ttk.LabelFrame(outer, text="ログ", padding=4)
        log_frame.grid(row=2, column=0, sticky="ew")
        log_frame.columnconfigure(0, weight=1)
        self.log_text = tk.Text(log_frame, height=3, wrap="word")
        self.log_text.grid(row=0, column=0, sticky="ew")
        log_scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        log_scroll.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=log_scroll.set)
        self.append_log("標準フィルターを読み込みました")
        if self.initial_rule_reclass_stats.get("reclassified"):
            self.append_log(
                f"譜面傾向の判定設定を更新し、{self.initial_rule_reclass_stats['reclassified']:,}譜面へ反映しました"
            )
        if self.analysis_rule_reclass_error:
            self.append_log("保存済み解析結果の更新に失敗しました: " + self.analysis_rule_reclass_error)
        self.refresh_simple_filter_sets()
        self.update_analysis_target_summary()
        self.update_pending_analysis_status()

    def show_existing_folder_visibility(self) -> None:
        if not self.env:
            messagebox.showerror(APP_NAME, "先にbeatoraja環境を読み込んでください", parent=self.root)
            return
        dialog = ExistingFolderVisibilityDialog(self.root, self)
        self.root.wait_window(dialog)

    def build_env_tab(self) -> None:
        row = ttk.Frame(self.env_tab)
        row.pack(fill="x")
        ttk.Label(row, text="beatoraja本体フォルダ").pack(side="left")
        ttk.Entry(row, textvariable=self.root_var).pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(row, text="参照", command=self.browse_root).pack(side="left", padx=2)
        ttk.Button(row, text="自動検出", command=self.auto_detect_root).pack(side="left", padx=2)
        ttk.Button(row, text="読込", command=self.load_environment).pack(side="left", padx=2)

        history_row = ttk.Frame(self.env_tab)
        history_row.pack(fill="x", pady=(8, 0))
        ttk.Label(history_row, text="history_cursong.json（任意）").pack(side="left")
        ttk.Entry(history_row, textvariable=self.history_path_var).pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(history_row, text="参照", command=self.browse_history_path).pack(side="left")

        info = ttk.LabelFrame(self.env_tab, text="検出結果", padding=10)
        info.pack(fill="x", pady=12)
        ttk.Label(info, textvariable=self.env_text_var, justify="left").pack(anchor="w")

        note = (
            "このツールはfolder/default.json直下へ設定した難易度表フォルダを追加し、songdata.dbへ専用の"
            " bmscf_* テーブルを作成します。既存の一般テーブルは変更しません。\n"
            "ランプ・スコア・ミスカウント・プレイ回数・最終プレイ日時は、beatorajaがフォルダを開くたび"
            "score.dbへ直接問い合わせるため自動反映されます。JSON構造を変えた場合だけbeatoraja再起動が必要です。"
        )
        ttk.Label(self.env_tab, text=note, wraplength=920, justify="left").pack(anchor="w", pady=8)

        actions = ttk.Frame(self.env_tab)
        actions.pack(fill="x", pady=6)
        ttk.Button(actions, text="folderフォルダを開く", command=self.open_folder_dir).pack(side="left", padx=3)
        ttk.Button(actions, text="tableフォルダを開く", command=self.open_table_dir).pack(side="left", padx=3)
        ttk.Button(actions, text="設定ファイルを開く", command=self.open_config).pack(side="left", padx=3)

    def build_table_tab(self) -> None:
        columns = ("role", "source", "status", "scale", "levels", "charts", "name", "url")
        tree = ttk.Treeview(self.table_tab, columns=columns, show="headings", selectmode="extended")
        self.table_tree = tree
        headings = {
            "role": "構成利用",
            "source": "取得元",
            "status": "状態",
            "scale": "難度基準",
            "levels": "レベル",
            "charts": "譜面",
            "name": "表名",
            "url": "URL",
        }
        widths = {"role": 105, "source": 75, "status": 90, "scale": 150, "levels": 60, "charts": 65, "name": 210, "url": 330}
        for key in columns:
            tree.heading(key, text=headings[key])
            tree.column(key, width=widths[key], stretch=key in {"name", "url"})
        tree.pack(fill="both", expand=True)

        toolbar = ttk.Frame(self.table_tab)
        toolbar.pack(fill="x", pady=8)
        ttk.Button(toolbar, text="再読込", command=lambda: self.reload_tables(fetch_missing=False)).pack(side="left", padx=3)
        ttk.Button(toolbar, text="未取得表をURL取得", command=lambda: self.reload_tables(fetch_missing=True)).pack(side="left", padx=3)
        ttk.Button(toolbar, text="URL追加", command=self.add_table).pack(side="left", padx=3)
        ttk.Button(toolbar, text="ツール登録から削除", command=self.remove_app_table).pack(side="left", padx=3)

        ttk.Label(
            self.table_tab,
            text="表の取得・登録を管理します。実際の掛け合わせ方と表示フォルダ名は『フォルダ構成』タブで設定します。",
        ).pack(anchor="w")

    def build_structure_tab(self) -> None:
        top = ttk.LabelFrame(self.structure_tab, text="掛け合わせ済み難易度表（default.json直下へ生成）", padding=7)
        top.pack(fill="both", expand=True)
        columns = ("visible", "name", "method", "inputs")
        tree = ttk.Treeview(top, columns=columns, show="headings", selectmode="extended", height=10)
        self.combination_tree = tree
        for key, label, width in (
            ("visible", "表示", 55), ("name", "フォルダ名", 270),
            ("method", "掛け合わせ方", 230), ("inputs", "入力（順番）", 470),
        ):
            tree.heading(key, text=label)
            tree.column(key, width=width, stretch=key in {"name", "inputs"})
        tree.pack(fill="both", expand=True)
        tree.bind("<Double-1>", lambda _e: self.edit_combination())
        bar = ttk.Frame(top)
        bar.pack(fill="x", pady=(7, 0))
        ttk.Button(bar, text="追加", command=self.add_combination).pack(side="left", padx=2)
        ttk.Button(bar, text="編集", command=self.edit_combination).pack(side="left", padx=2)
        ttk.Button(bar, text="複製", command=self.copy_combination).pack(side="left", padx=2)
        ttk.Button(bar, text="削除", command=self.delete_combinations).pack(side="left", padx=2)
        ttk.Button(bar, text="↑", width=3, command=lambda: self.move_combination(-1)).pack(side="left", padx=(10, 2))
        ttk.Button(bar, text="↓", width=3, command=lambda: self.move_combination(1)).pack(side="left", padx=2)
        ttk.Button(bar, text="選択を表示", command=lambda: self.set_combination_visibility(True)).pack(side="right", padx=2)
        ttk.Button(bar, text="選択を非表示", command=lambda: self.set_combination_visibility(False)).pack(side="right", padx=2)
        ttk.Label(
            top,
            text=(
                "元難易度表と生成済み難易度表を同じ入力として扱えます。複数入力・多段合成に対応し、"
                "非表示の中間表も派生表の素材として保持します。"
            ),
            foreground="#555555",
        ).pack(anchor="w", pady=(6, 0))

        random_box = ttk.LabelFrame(self.structure_tab, text="各レベル・フィルターのランダムコース", padding=7)
        random_box.pack(fill="x", pady=(10, 0))
        ttk.Checkbutton(
            random_box, text="各レベルに『すべて』を抽選元とするランダムコースを生成",
            variable=self.random_course_enabled_var,
        ).grid(row=0, column=0, columnspan=4, sticky="w")
        ttk.Checkbutton(
            random_box, text="平均密度・プレイ状況など、各フィルターにもランダムコースを生成",
            variable=self.random_course_filter_enabled_var,
        ).grid(row=1, column=0, columnspan=4, sticky="w", pady=(3, 0))

        ttk.Label(random_box, text="格納フォルダ名").grid(row=2, column=0, sticky="w", pady=(6, 0))
        ttk.Entry(random_box, textvariable=self.random_course_folder_name_var, width=20).grid(
            row=2, column=1, sticky="w", padx=(4, 14), pady=(6, 0)
        )
        ttk.Label(random_box, text="曲数").grid(row=2, column=2, sticky="w", pady=(6, 0))
        ttk.Spinbox(
            random_box, from_=1, to=20, width=6, textvariable=self.random_course_stages_var,
        ).grid(row=2, column=3, sticky="w", padx=(4, 8), pady=(6, 0))

        ttk.Label(random_box, text="ALL名称").grid(row=3, column=0, sticky="w", pady=(6, 0))
        ttk.Entry(random_box, textvariable=self.random_course_name_var, width=34).grid(
            row=3, column=1, columnspan=3, sticky="ew", padx=(4, 8), pady=(6, 0)
        )
        ttk.Label(random_box, text="フィルター名称").grid(row=4, column=0, sticky="w", pady=(6, 0))
        ttk.Entry(random_box, textvariable=self.random_course_filter_name_var, width=34).grid(
            row=4, column=1, columnspan=3, sticky="ew", padx=(4, 8), pady=(6, 0)
        )
        random_box.columnconfigure(3, weight=1)
        ttk.Checkbutton(
            random_box, text="同一譜面の重複を避ける", variable=self.random_course_distinct_var,
        ).grid(row=5, column=0, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Button(random_box, text="設定を保存", command=self.save_settings_now).grid(
            row=5, column=3, sticky="e", pady=(6, 0)
        )
        ttk.Label(
            random_box,
            text=(
                "生成済みコースは『格納フォルダ名』の中だけへ蓄積されます。"
                "名称では {count}=実際の曲数、{level}=レベル名、フィルター名称ではさらに "
                "{group}=親階層、{filter}=フィルター名を使用できます。"
            ),
            foreground="#666666",
            wraplength=930,
            justify="left",
        ).grid(row=6, column=0, columnspan=4, sticky="w", pady=(5, 0))

        bottom = ttk.LabelFrame(self.structure_tab, text="既存のfolder/default.json項目", padding=7)
        bottom.pack(fill="both", expand=True, pady=(10, 0))
        ttk.Label(
            bottom,
            textvariable=self.default_profile_var,
            foreground="#2b579a",
        ).pack(anchor="w", pady=(0, 6))
        columns = ("visible", "name", "kind")
        dtree = ttk.Treeview(bottom, columns=columns, show="headings", selectmode="extended", height=7)
        self.default_folder_tree = dtree
        for key, label, width in (("visible", "表示", 55), ("name", "フォルダ名", 430), ("kind", "種類", 150)):
            dtree.heading(key, text=label)
            dtree.column(key, width=width, stretch=key == "name")
        dtree.pack(fill="both", expand=True)
        dbar = ttk.Frame(bottom)
        dbar.pack(fill="x", pady=(7, 0))
        ttk.Button(dbar, text="default.jsonから再読込", command=self.reload_default_folder_catalog).pack(side="left", padx=2)
        ttk.Button(dbar, text="選択を表示", command=lambda: self.set_default_folder_visibility(True)).pack(side="right", padx=2)
        ttk.Button(dbar, text="選択を非表示", command=lambda: self.set_default_folder_visibility(False)).pack(side="right", padx=2)
        ttk.Label(
            bottom,
            text="表示／非表示は読み込んだbeatoraja本体フォルダーごとに保存されます。反映にはカスタムフォルダーの更新が必要です。",
            foreground="#555555",
        ).pack(anchor="w", pady=(6, 0))
        self.refresh_structure()

    def build_scale_tab(self) -> None:
        columns = ("status", "symbols", "levels", "mapped", "name")
        tree = ttk.Treeview(self.scale_tab, columns=columns, show="headings", selectmode="browse", height=13)
        self.scale_tree = tree
        headings = {
            "status": "状態", "symbols": "記号", "levels": "レベル数",
            "mapped": "共通軸対応", "name": "表名",
        }
        widths = {"status": 75, "symbols": 95, "levels": 75, "mapped": 100, "name": 360}
        for key in columns:
            tree.heading(key, text=headings[key])
            tree.column(key, width=widths[key], stretch=key == "name")
        tree.pack(fill="x")
        tree.bind("<<TreeviewSelect>>", lambda _e: self.show_selected_scale())
        tree.bind("<Double-1>", lambda _e: self.open_selected_scale_source())

        toolbar = ttk.Frame(self.scale_tab)
        toolbar.pack(fill="x", pady=7)
        ttk.Button(toolbar, text="難度換算JSONを開く", command=lambda: open_path(difficulty_registry_path())).pack(side="left", padx=3)
        ttk.Button(toolbar, text="即席密度JSONを開く", command=lambda: open_path(instant_density_profile_path())).pack(side="left", padx=3)
        ttk.Button(toolbar, text="選択表の出典を開く", command=self.open_selected_scale_source).pack(side="left", padx=3)
        ttk.Button(toolbar, text="再読込", command=self.reload_difficulty_registry).pack(side="left", padx=3)
        ttk.Label(
            toolbar,
            text="換算は範囲・確度・出典を保持し、根拠のない表は未換算のままにします。",
            foreground="#666666",
        ).pack(side="right")

        profile_box = ttk.LabelFrame(self.scale_tab, text="表外差分即席難易度表の密度順位基準", padding=6)
        profile_box.pack(fill="x", pady=(0, 7))
        ttk.Label(profile_box, textvariable=self.instant_profile_var, wraplength=900).pack(anchor="w")
        self.scale_detail = tk.Text(self.scale_tab, height=16, wrap="word")
        self.scale_detail.pack(fill="both", expand=True)
        self.scale_detail.configure(state="disabled")
        self.refresh_difficulty_registry()

    def reload_difficulty_registry(self) -> None:
        try:
            self.difficulty_registry = load_difficulty_scale_registry()
            self.instant_density_profile = load_instant_density_profiles()
            self.registry_error = ""
            self.instant_profile_error = ""
            self.instant_profile_var.set(instant_density_profile_summary(self.instant_density_profile))
            self.refresh_difficulty_registry()
            self.status_var.set("同梱の難度基準を再読込しました")
        except Exception as exc:
            self.registry_error = str(exc)
            messagebox.showerror(APP_NAME, str(exc), parent=self.root)

    def refresh_difficulty_registry(self) -> None:
        if not self.scale_tree:
            return
        self.scale_tree.delete(*self.scale_tree.get_children())
        for entry in self.difficulty_registry.get("tables", []):
            level_count = len(entry.get("level_order", []))
            mapped_count = len(entry.get("mappings", []))
            self.scale_tree.insert(
                "", "end", iid=str(entry.get("id")),
                values=(
                    entry.get("status", "—"),
                    " / ".join(str(x) for x in entry.get("symbols", [])) or "—",
                    level_count,
                    f"{mapped_count}/{level_count}" if level_count else mapped_count,
                    entry.get("name", entry.get("id", "")),
                ),
            )
        if self.registry_error:
            self.set_scale_detail("難度基準レジストリの読込に失敗しました。\n\n" + self.registry_error)
        elif self.scale_tree.get_children():
            first = self.scale_tree.get_children()[0]
            self.scale_tree.selection_set(first)
            self.show_selected_scale()

    def selected_scale_entry(self) -> dict[str, Any] | None:
        if not self.scale_tree:
            return None
        selected = self.scale_tree.selection()
        if not selected:
            return None
        target = selected[0]
        return next((x for x in self.difficulty_registry.get("tables", []) if str(x.get("id")) == target), None)

    def set_scale_detail(self, text: str) -> None:
        if not self.scale_detail:
            return
        self.scale_detail.configure(state="normal")
        self.scale_detail.delete("1.0", "end")
        self.scale_detail.insert("1.0", text)
        self.scale_detail.configure(state="disabled")

    def show_selected_scale(self) -> None:
        entry = self.selected_scale_entry()
        if not entry:
            return
        source_map = difficulty_registry_sources(self.difficulty_registry)
        family_map = {
            str(x.get("id")): str(x.get("label") or x.get("id"))
            for x in self.difficulty_registry.get("same_number_families", [])
            if isinstance(x, dict) and x.get("id")
        }
        same_family = str(entry.get("same_number_family") or "")
        lines = [
            str(entry.get("name") or entry.get("id")),
            "=" * 60,
            f"ID: {entry.get('id')}",
            f"記号: {' / '.join(str(x) for x in entry.get('symbols', [])) or '—'}",
            f"状態: {entry.get('status', '—')}",
            f"同番号系列: {family_map.get(same_family, '—')}",
            f"基準: {difficulty_scale_summary(entry)}",
            f"レベル順: {', '.join(str(x) for x in entry.get('level_order', [])) or '未登録'}",
            "",
            "査定・運用基準:",
            json.dumps(entry.get("basis", {}), ensure_ascii=False, indent=2),
            "",
            "共通軸対応:",
        ]
        if entry.get("mappings"):
            for mapping in entry.get("mappings", []):
                lo, hi = mapping.get("axis_min"), mapping.get("axis_max")
                axis = str(lo) if lo == hi else f"{lo}〜{hi}"
                usable = "" if mapping.get("usable_for_gap_detection", True) else " / 空白判定では既定除外"
                note = f" / {mapping.get('note')}" if mapping.get("note") else ""
                lines.append(f"- {mapping.get('level')}: 軸{axis} / {mapping.get('confidence', 'unknown')}{usable}{note}")
        else:
            lines.append("- 他表との換算根拠が不足しているため未設定")
        if entry.get("special_levels"):
            lines.extend(["", "特殊・未換算レベル: " + ", ".join(str(x) for x in entry.get("special_levels", []))])
        if entry.get("notes"):
            lines.extend(["", "注記:"] + [f"- {x}" for x in entry.get("notes", [])])
        lines.extend(["", "出典:"])
        source_ids = list(dict.fromkeys([*entry.get("source_ids", []), *[sid for m in entry.get("mappings", []) for sid in m.get("source_ids", [])]]))
        for sid in source_ids:
            source = source_map.get(str(sid), {})
            if source:
                lines.append(f"- {source.get('title')}\n  {source.get('url')}\n  確認日: {source.get('checked_at')} / {source.get('note', '')}")
        self.set_scale_detail("\n".join(lines))

    def open_selected_scale_source(self) -> None:
        entry = self.selected_scale_entry()
        if not entry:
            messagebox.showinfo(APP_NAME, "難度基準を選択してください", parent=self.root)
            return
        source_map = difficulty_registry_sources(self.difficulty_registry)
        for sid in entry.get("source_ids", []):
            source = source_map.get(str(sid))
            if source and source.get("url"):
                open_url(str(source["url"]))
                return
        messagebox.showinfo(APP_NAME, "開ける出典URLがありません", parent=self.root)


    def build_preset_tab(self) -> None:
        columns = ("visible", "group", "join", "conditions", "name")
        tree = ttk.Treeview(self.preset_tab, columns=columns, show="headings", selectmode="extended")
        self.preset_tree = tree
        tree.heading("visible", text="表示")
        tree.heading("group", text="親グループ")
        tree.heading("join", text="結合")
        tree.heading("conditions", text="条件数")
        tree.heading("name", text="名称")
        tree.column("visible", width=60, anchor="center")
        tree.column("group", width=170)
        tree.column("join", width=70, anchor="center")
        tree.column("conditions", width=70, anchor="center")
        tree.column("name", width=330)
        tree.pack(fill="both", expand=True)
        tree.bind("<Double-1>", lambda _e: self.edit_preset())
        tree.bind("<<TreeviewSelect>>", lambda _e: self.update_preset_summary())
        tree.bind("<Control-a>", self.select_all_presets)
        tree.bind("<Control-A>", self.select_all_presets)
        tree.bind("<Delete>", lambda _e: self.delete_preset())

        edit_tools = ttk.Frame(self.preset_tab)
        edit_tools.pack(fill="x", pady=(8, 3))
        ttk.Button(edit_tools, text="追加", command=self.add_preset).pack(side="left", padx=3)
        ttk.Button(edit_tools, text="編集", command=self.edit_preset).pack(side="left", padx=3)
        ttk.Button(edit_tools, text="複製", command=self.copy_preset).pack(side="left", padx=3)
        ttk.Button(edit_tools, text="↑", width=4, command=lambda: self.move_preset(-1)).pack(side="left", padx=3)
        ttk.Button(edit_tools, text="↓", width=4, command=lambda: self.move_preset(1)).pack(side="left", padx=3)
        ttk.Button(edit_tools, text="初期テンプレートを追加", command=self.add_initial_templates).pack(side="left", padx=(12, 3))
        ttk.Button(edit_tools, text="16/12×実質BPM一括生成", command=self.add_rhythm_bpm_bulk).pack(side="left", padx=3)
        ttk.Button(edit_tools, text="プレイ用小型パネル", command=self.quick_panel.show).pack(side="right", padx=3)

        bulk_tools = ttk.Frame(self.preset_tab)
        bulk_tools.pack(fill="x", pady=(0, 5))
        ttk.Button(bulk_tools, text="全選択", command=self.select_all_presets).pack(side="left", padx=3)
        ttk.Button(bulk_tools, text="詳細BPMを選択", command=self.select_legacy_bpm_presets).pack(side="left", padx=3)
        ttk.Button(bulk_tools, text="選択を表示", command=lambda: self.set_selected_presets_visible(True)).pack(side="left", padx=(12, 3))
        ttk.Button(bulk_tools, text="選択を非表示", command=lambda: self.set_selected_presets_visible(False)).pack(side="left", padx=3)
        ttk.Button(bulk_tools, text="選択を削除", command=self.delete_preset).pack(side="left", padx=3)
        ttk.Label(bulk_tools, textvariable=self.preset_summary_var).pack(side="right", padx=3)

        ttk.Label(
            self.preset_tab,
            text=(
                "Ctrl/Shiftで複数選択、Ctrl+Aで全選択できます。非表示プリセットは生成対象になりません。"
                "v0.6.0の詳細BPMテンプレートは初回移行時に非表示になります。構造変更後はJSON再生成とbeatoraja再起動が必要です。"
            ),
            wraplength=1000,
        ).pack(anchor="w")
        self.refresh_presets()

    def build_generate_tab(self) -> None:
        special = ttk.LabelFrame(self.generate_tab, text="追加機能", padding=8)
        special.pack(fill="x", pady=(0, 8))
        ttk.Checkbutton(
            special, text="表外差分即席難易度表（sr/sl/st帯・密度順位1～12）",
            variable=self.instant_enabled_var, command=self.save_settings_now,
        ).pack(anchor="w")
        ttk.Checkbutton(
            special, text="差分制作者向けフィルタ（各難易度表レベル内：差分不足・上下譜面・難度空白・制作素材）",
            variable=self.maker_enabled_var, command=self.save_settings_now,
        ).pack(anchor="w")
        ttk.Label(special, textvariable=self.instant_profile_var, foreground="#666666").pack(anchor="w", padx=(22, 0))
        ttk.Label(
            special,
            text=(
                "差分制作フィルタはst/sl等の通常難易度表にだけ適用します。"
                "制作素材系3フィルタを非表示にすると、生成時の音源走査を完全に省略します。"
            ),
            foreground="#666666",
            wraplength=1000,
        ).pack(anchor="w", padx=(22, 0), pady=(2, 0))

        summary = ttk.LabelFrame(self.generate_tab, text="生成内容", padding=10)
        summary.pack(fill="x")
        ttk.Label(summary, textvariable=self.preview_var).pack(anchor="w")
        tools = ttk.Frame(self.generate_tab)
        tools.pack(fill="x", pady=10)
        ttk.Button(tools, text="件数プレビュー", command=self.preview).pack(side="left", padx=3)
        ttk.Button(tools, text="JSON＋構成DBを生成", command=lambda: self.start_generate(True)).pack(side="left", padx=3)
        ttk.Button(tools, text="構成DBだけ更新（再起動不要）", command=lambda: self.start_generate(False)).pack(side="left", padx=3)
        ttk.Button(tools, text="譜面解析を更新", command=lambda: self.start_analysis(False)).pack(side="left", padx=3)
        ttk.Button(tools, text="プレイ用小型パネル", command=self.quick_panel.show).pack(side="left", padx=3)
        ttk.Button(tools, text="beatorajaへ戻る", command=focus_rian_window).pack(side="left", padx=3)

        progress_frame = ttk.LabelFrame(self.generate_tab, text="処理状況", padding=8)
        progress_frame.pack(fill="x", pady=(0, 8))
        header = ttk.Frame(progress_frame)
        header.pack(fill="x")
        ttk.Label(header, textvariable=self.progress_state_var, width=24).pack(side="left")
        ttk.Label(header, textvariable=self.progress_stage_var).pack(side="left", fill="x", expand=True)
        ttk.Label(header, textvariable=self.progress_percent_var, width=9, anchor="e").pack(side="right")
        self.progress_bar = ttk.Progressbar(
            progress_frame, variable=self.progress_value_var, maximum=100.0, mode="determinate"
        )
        self.progress_bar.pack(fill="x", pady=(5, 3))
        detail_row = ttk.Frame(progress_frame)
        detail_row.pack(fill="x")
        ttk.Label(detail_row, textvariable=self.progress_detail_var, foreground="#555555").pack(
            side="left", fill="x", expand=True
        )
        ttk.Label(detail_row, textvariable=self.progress_time_var).pack(side="right", padx=(12, 0))

        ttk.Label(
            self.generate_tab,
            text=(
                "初回、およびプリセットの追加・削除・項目・比較方法・AND/OR・表示構造を変更した後は"
                "『JSON＋構成DBを生成』を実行してbeatorajaを再起動してください。ランプやスコア等のプレイ記録は"
                "フォルダを開き直すだけで自動反映されます。小型パネルでは、経過日数・最小BP・スコア率の値を即時変更できます。"
            ),
            wraplength=920,
            justify="left",
        ).pack(anchor="w", pady=(0, 8))

        self.log_text = tk.Text(self.generate_tab, height=17, wrap="word")
        self.log_text.pack(fill="both", expand=True)
        self.append_log("準備完了")

    def _analysis_rule_pack_summary(self) -> str:
        if self.analysis_rule_pack_error:
            return "譜面傾向設定: 読込失敗 — 標準設定を使用中"
        info = get_active_rule_pack_info()
        label = str(info.get("name") or info.get("pack_id") or "譜面傾向設定")
        version = str(info.get("version") or "").strip()
        return f"譜面傾向設定: {label}" + (f" v{version}" if version else "")

    def open_analysis_rule_pack_dir(self) -> None:
        ANALYSIS_RULE_PACK_DIR.mkdir(parents=True, exist_ok=True)
        try:
            open_path(ANALYSIS_RULE_PACK_DIR)
        except Exception as exc:
            messagebox.showerror(APP_NAME, str(exc), parent=self.root)

    def import_analysis_rule_pack(self) -> None:
        raw_path = filedialog.askopenfilename(
            parent=self.root,
            title="追加・更新する譜面傾向設定JSONを選択",
            filetypes=[("譜面傾向設定", "*.json"), ("すべてのファイル", "*.*")],
        )
        if not raw_path:
            return
        source = Path(raw_path)
        try:
            if source.suffix.lower() != ".json":
                raise ToolError("JSONファイルではありません")
            with tempfile.TemporaryDirectory() as td:
                temporary = Path(td) / source.name
                shutil.copy2(source, temporary)
                pack = load_rule_pack_file(temporary)
                pack_id = str(pack.get("pack_id") or "")
                ANALYSIS_RULE_PACK_DIR.mkdir(parents=True, exist_ok=True)
                backup_dir = ANALYSIS_RULE_PACK_DIR / "backup"
                backup_dir.mkdir(parents=True, exist_ok=True)
                stamp = time.strftime("%Y%m%d_%H%M%S")
                for old in ANALYSIS_RULE_PACK_DIR.glob("*.json"):
                    try:
                        old_pack = load_rule_pack_file(old)
                        old_id = str(old_pack.get("pack_id") or "")
                    except Exception:
                        old_id = ""
                    backup = backup_dir / f"{old.stem}_{stamp}{old.suffix}"
                    shutil.copy2(old, backup)
                    old.unlink()
                shutil.copy2(temporary, ANALYSIS_RULE_PACK_DIR / source.name)
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"譜面傾向設定を追加できませんでした。\n\n{exc}", parent=self.root)
            return
        self.reload_analysis_rule_pack(show_dialog=True)

    def reload_analysis_rule_pack(self, show_dialog: bool = True) -> None:
        try:
            reload_active_rule_pack(ANALYSIS_RULE_PACK_DIR)
            self.analysis_rule_pack_error = ""
            self.analysis_rule_pack_summary_var.set(self._analysis_rule_pack_summary())
            stats = reclassify_chart_analysis(CHART_ANALYSIS_DB, force=False)
            message = (
                f"譜面傾向の判定設定を再読込しました。\n\n"
                f"更新した差分: {stats.get('reclassified', 0):,}譜面\n"
                f"変更なし: {stats.get('cached', 0):,}譜面"
            )
            self.append_log(message.replace("\n", " / "))
            self.set_quick_status(
                f"譜面傾向設定更新: {stats.get('reclassified', 0):,}譜面へ反映"
            )
            if show_dialog:
                messagebox.showinfo(APP_NAME, message, parent=self.root)
        except Exception as exc:
            self.analysis_rule_pack_error = str(exc)
            self.analysis_rule_pack_summary_var.set(self._analysis_rule_pack_summary())
            messagebox.showerror(APP_NAME, f"譜面傾向の判定設定を読み込めませんでした。\n\n{exc}", parent=self.root)

    def _filter_pack_summary(self) -> str:
        stats = getattr(self, "filter_pack_stats", {}) or {}
        if not stats.get("applied"):
            messages = stats.get("messages") or []
            return "フィルター設定: 読込失敗" + (f" — {messages[0]}" if messages else "")
        infos = stats.get("pack_infos") or []
        labels = []
        for info in infos:
            version = str(info.get("version") or "").strip()
            labels.append(str(info.get("name") or info.get("pack_id") or "設定") + (f" v{version}" if version else ""))
        return f"フィルター設定: {', '.join(labels)} / {stats.get('loaded', 0)}フィルター"

    def open_filter_pack_dir(self) -> None:
        FILTER_PACK_DIR.mkdir(parents=True, exist_ok=True)
        try:
            open_path(FILTER_PACK_DIR)
        except Exception as exc:
            messagebox.showerror(APP_NAME, str(exc), parent=self.root)

    def import_filter_packs(self) -> None:
        paths = filedialog.askopenfilenames(
            parent=self.root,
            title="追加・更新するフィルター設定JSONを選択",
            filetypes=[("フィルター設定", "*.json"), ("すべてのファイル", "*.*")],
        )
        if not paths:
            return
        FILTER_PACK_DIR.mkdir(parents=True, exist_ok=True)
        selected: list[tuple[Path, str]] = []
        pack_ids: set[str] = set()
        try:
            for raw_path in paths:
                source = Path(raw_path)
                if source.suffix.lower() != ".json":
                    raise ToolError(f"JSONファイルではありません: {source.name}")
                with tempfile.TemporaryDirectory() as td:
                    temporary = Path(td) / source.name
                    shutil.copy2(source, temporary)
                    _presets, messages, files, infos = load_filter_preset_packs(Path(td))
                if messages or files != 1 or len(infos) != 1:
                    raise ToolError("\n".join(messages or [f"フィルター設定として読み込めません: {source.name}"]))
                pack_id = str(infos[0].get("pack_id") or "")
                if pack_id in pack_ids:
                    raise ToolError(f"選択したファイル内でpack_idが重複しています: {pack_id}")
                pack_ids.add(pack_id)
                selected.append((source, pack_id))

            backup_dir = FILTER_PACK_DIR / "backup"
            backup_dir.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y%m%d_%H%M%S")
            existing_by_pack: dict[str, list[Path]] = {}
            for existing in FILTER_PACK_DIR.glob("*.json"):
                try:
                    raw = json.loads(existing.read_text(encoding="utf-8-sig"))
                    pack_id = str(raw.get("pack_id") or existing.stem).strip() if isinstance(raw, dict) else ""
                except Exception:
                    pack_id = ""
                if pack_id:
                    existing_by_pack.setdefault(pack_id, []).append(existing)

            copied = 0
            for source, pack_id in selected:
                target = FILTER_PACK_DIR / source.name
                for old in existing_by_pack.get(pack_id, []):
                    if old.resolve() == target.resolve():
                        continue
                    backup = backup_dir / f"{old.stem}_{stamp}{old.suffix}"
                    shutil.copy2(old, backup)
                    old.unlink()
                if target.exists() and source.resolve() != target.resolve():
                    backup = backup_dir / f"{target.stem}_{stamp}{target.suffix}"
                    shutil.copy2(target, backup)
                if source.resolve() != target.resolve():
                    shutil.copy2(source, target)
                copied += 1
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"フィルター設定を追加・更新できませんでした。\n\n{exc}", parent=self.root)
            return
        self.reload_filter_packs()
        self.set_quick_status(f"フィルター設定 {copied}件を追加・更新しました")

    def reload_filter_packs(self, reset_values: bool = False) -> None:
        if reset_values and not messagebox.askyesno(
            APP_NAME,
            "変更した数値を破棄し、標準の既定値へ戻しますか？\n"
            "各フィルターのON/OFFは維持します。",
            parent=self.root,
        ):
            return
        if self.simple_filter_editor is not None:
            self.simple_filter_editor.commit_all()
        try:
            old_enabled = dict(self.settings.simple_preset_enabled)
            presets, stats = sync_filter_preset_packs(
                self.settings.presets,
                FILTER_PACK_DIR,
                preserve_numeric_values=not reset_values,
                pack_only=True,
            )
            if not stats.get("applied"):
                self.filter_pack_stats = stats
                self.filter_pack_summary_var.set(self._filter_pack_summary())
                raise ToolError("\n".join(stats.get("messages") or ["フィルター設定を読み込めませんでした"]))
            self.settings.presets = presets
            self.filter_pack_stats = stats
            active_ids = {preset.preset_id for preset in presets}
            self.settings.simple_preset_enabled = {}
            for preset in presets:
                enabled = old_enabled.get(preset.preset_id, bool(preset.visible))
                self.settings.simple_preset_enabled[preset.preset_id] = bool(enabled)
                preset.visible = bool(enabled)
            self.settings.simple_preset_enabled = {
                key: value for key, value in self.settings.simple_preset_enabled.items() if key in active_ids
            }
            self.filter_pack_summary_var.set(self._filter_pack_summary())
            self.refresh_simple_filter_sets()
            self.save_settings_now()
            self.set_quick_status(
                f"フィルター設定再読込: 追加{stats['added']} / 更新{stats['updated']} / 削除{stats['removed']}"
            )
        except Exception as exc:
            messagebox.showerror(APP_NAME, str(exc), parent=self.root)

    def ensure_simple_filter_edit_set(self) -> str:
        sets = ordered_simple_filter_sets(self.settings)
        current = str(self.settings.simple_filter_set or "").strip()
        if current not in sets:
            current = next(
                (name for name in self.settings.simple_selected_filter_sets if name in sets),
                sets[0] if sets else "",
            )
            self.settings.simple_filter_set = current
        self.simple_filter_set_var.set(current)
        return current

    def set_simple_filter_edit_set(self, name: str | None = None) -> None:
        sets = ordered_simple_filter_sets(self.settings)
        target = str(name if name is not None else self.simple_filter_set_var.get()).strip()
        if target not in sets:
            target = sets[0] if sets else ""
        self.settings.simple_filter_set = target
        self.simple_filter_set_var.set(target)
        self.save_settings_now()
        if self.simple_filter_editor is not None:
            self.simple_filter_editor.rebuild(target)
        if hasattr(self, "quick_panel") and self.quick_panel is not None:
            self.quick_panel.refresh_presets()

    def refresh_simple_filter_sets(self) -> None:
        initialize_simple_filter_set_selection(self.settings)
        tree = self.simple_filter_set_tree
        sets = ordered_simple_filter_sets(self.settings)
        if tree is not None:
            selected_rows = set(tree.selection())
            tree.delete(*tree.get_children())
            enabled = set(self.settings.simple_selected_filter_sets)
            for index, name in enumerate(sets):
                iid = f"set:{index}"
                tree.insert(
                    "", "end", iid=iid,
                    values=(
                        "✓" if name in enabled else "—",
                        len(simple_presets_for_set(self.settings.presets, name)),
                        name,
                    ),
                    tags=(name,),
                )
            restore = [iid for iid in selected_rows if tree.exists(iid)]
            if restore:
                tree.selection_set(*restore)
        current = self.ensure_simple_filter_edit_set()
        if self.simple_filter_set_combo is not None:
            self.simple_filter_set_combo.configure(values=sets)
        self.update_simple_filter_set_summary()
        if self.simple_filter_editor is not None:
            self.simple_filter_editor.rebuild(current)
        self.quick_panel.refresh_presets()

    def _filter_set_name_from_iid(self, iid: str) -> str:
        tree = self.simple_filter_set_tree
        if tree is None or not tree.exists(iid):
            return ""
        values = tree.item(iid, "values")
        return str(values[2]) if len(values) >= 3 else ""

    def set_simple_selected_filter_sets(self, names: list[str]) -> None:
        available = ordered_simple_filter_sets(self.settings)
        requested = {str(name).strip() for name in names if str(name).strip()}
        ordered = [name for name in available if name in requested]
        self.settings.simple_selected_filter_sets = ordered
        self.settings.simple_filter_set_selection_initialized = True
        self.settings.simple_known_filter_sets = list(available)
        self.ensure_simple_filter_edit_set()
        self.save_settings_now()
        self.refresh_simple_filter_sets()

    def update_simple_filter_set_summary(self) -> None:
        available = set(ordered_simple_filter_sets(self.settings))
        count = sum(1 for name in self.settings.simple_selected_filter_sets if name in available)
        self.simple_filter_set_summary_var.set(f"フィルターセット: {count}件選択")

    def on_simple_filter_set_click(self, event: tk.Event) -> None:
        tree = self.simple_filter_set_tree
        if tree is None or tree.identify_column(event.x) != "#1":
            return
        row = tree.identify_row(event.y)
        if not row:
            return
        tree.selection_set(row)
        self.root.after_idle(self.toggle_simple_filter_set_selection)

    def toggle_simple_filter_set_selection(self) -> None:
        tree = self.simple_filter_set_tree
        if tree is None:
            return
        current = set(self.settings.simple_selected_filter_sets)
        for iid in tree.selection():
            name = self._filter_set_name_from_iid(iid)
            if not name:
                continue
            if name in current:
                current.remove(name)
            else:
                current.add(name)
        self.set_simple_selected_filter_sets(list(current))

    def set_all_simple_filter_sets(self, enabled: bool) -> None:
        names = ordered_simple_filter_sets(self.settings) if enabled else []
        self.set_simple_selected_filter_sets(names)

    def show_simple_filter_set_menu(self, event: tk.Event) -> str:
        tree = self.simple_filter_set_tree
        if tree is None or self.simple_filter_set_menu is None:
            return "break"
        row = tree.identify_row(event.y)
        if row and row not in tree.selection():
            tree.selection_set(row)
        try:
            self.simple_filter_set_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.simple_filter_set_menu.grab_release()
        return "break"

    def set_selected_simple_filter_sets(self, enabled: bool) -> None:
        tree = self.simple_filter_set_tree
        if tree is None:
            return
        current = set(self.settings.simple_selected_filter_sets)
        selected_names = {self._filter_set_name_from_iid(iid) for iid in tree.selection()}
        selected_names.discard("")
        if enabled:
            current.update(selected_names)
        else:
            current.difference_update(selected_names)
        self.set_simple_selected_filter_sets(list(current))

    def move_simple_filter_set(self, delta: int) -> None:
        tree = self.simple_filter_set_tree
        if tree is None:
            return
        selected = list(tree.selection())
        if len(selected) != 1:
            messagebox.showinfo(APP_NAME, "並べ替えるフィルターセットを1件選択してください", parent=self.root)
            return
        name = self._filter_set_name_from_iid(selected[0])
        order = ordered_simple_filter_sets(self.settings)
        if name not in order:
            return
        index = order.index(name)
        new_index = index + delta
        if not 0 <= new_index < len(order):
            return
        order[index], order[new_index] = order[new_index], order[index]
        self.settings.simple_filter_set_order = order
        self.save_settings_now()
        self.refresh_simple_filter_sets()
        target_iid = next((iid for iid in tree.get_children() if self._filter_set_name_from_iid(iid) == name), "")
        if target_iid:
            tree.selection_set(target_iid)
            tree.focus(target_iid)
            tree.see(target_iid)

    def filter_settings_changed(self, source: object | None = None) -> None:
        self.save_settings_now()
        if self.preset_tree:
            self.refresh_presets(refresh_quick=False)
        # 数値を変更したフィルター名は、その場で一覧・プルダウン・出力名へ反映する。
        self.refresh_simple_filter_sets()
        self.set_quick_status("設定を保存しました")

    def set_quick_status(self, text: str) -> None:
        try:
            self.quick_panel.status_var.set(text)
        except Exception:
            pass
        self.status_var.set(text)

    def update_simple_table_summary(self) -> None:
        valid = {table.table_id for table in self.tables if not table.error and table.levels}
        count = sum(1 for table_id in self.settings.simple_selected_table_ids if table_id in valid)
        self.simple_table_summary_var.set(f"対象表: {count}件選択")

    def update_analysis_target_summary(self) -> None:
        selected = set(self.settings.simple_selected_table_ids)
        charts: set[str] = set()
        table_count = 0
        for table in self.tables:
            if table.table_id not in selected or table.error or not table.levels:
                continue
            table_count += 1
            charts.update(table.all_charts().keys())
        if table_count == 0:
            self.analysis_target_var.set("解析対象: 対象難易度表を1件以上ONにしてください")
        else:
            self.analysis_target_var.set(
                f"解析対象: {table_count}表 / 最大 {len(charts):,}差分（同一差分は重複除外）"
            )

    def refresh_simple_tables(self) -> None:
        tree = self.simple_table_tree
        if tree is None:
            self.update_simple_table_summary()
            return
        selected_rows = set(tree.selection())
        tree.delete(*tree.get_children())
        enabled = set(self.settings.simple_selected_table_ids)
        for table in self.tables:
            if table.error or not table.levels:
                continue
            tree.insert(
                "", "end", iid=table.table_id,
                values=("✓" if table.table_id in enabled else "—", table.chart_count, table.name),
            )
        restore = [table_id for table_id in selected_rows if tree.exists(table_id)]
        if restore:
            tree.selection_set(*restore)
        self._apply_simple_table_sort()
        self.update_simple_table_summary()
        self.update_analysis_target_summary()

    def sort_simple_tables(self, column: str) -> None:
        if self.simple_table_sort_column == column:
            self.simple_table_sort_reverse = not self.simple_table_sort_reverse
        else:
            self.simple_table_sort_column = column
            self.simple_table_sort_reverse = False
        self._apply_simple_table_sort()

    def _apply_simple_table_sort(self) -> None:
        tree = self.simple_table_tree
        if tree is None:
            return
        column = self.simple_table_sort_column
        index_map = {"selected": 0, "charts": 1, "name": 2}
        index = index_map.get(column, 2)
        items = list(tree.get_children())
        def key(iid: str):
            values = tree.item(iid, "values")
            value = values[index] if len(values) > index else ""
            if column == "charts":
                try:
                    return int(str(value).replace(",", ""))
                except ValueError:
                    return 0
            if column == "selected":
                return 1 if str(value) == "✓" else 0
            return str(value).casefold()
        items.sort(key=key, reverse=self.simple_table_sort_reverse)
        for position, iid in enumerate(items):
            tree.move(iid, "", position)

    def show_simple_table_menu(self, event: tk.Event) -> str:
        tree = self.simple_table_tree
        if tree is None or self.simple_table_menu is None:
            return "break"
        row = tree.identify_row(event.y)
        if row and row not in tree.selection():
            tree.selection_set(row)
        try:
            self.simple_table_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.simple_table_menu.grab_release()
        return "break"

    def set_selected_simple_tables(self, enabled: bool) -> None:
        tree = self.simple_table_tree
        if tree is None:
            return
        current = set(self.settings.simple_selected_table_ids)
        selected = set(tree.selection())
        if enabled:
            current.update(selected)
        else:
            current.difference_update(selected)
        self.set_simple_selected_tables(list(current))

    def set_simple_selected_tables(self, table_ids: list[str]) -> None:
        valid = {table.table_id for table in self.tables if not table.error and table.levels}
        ordered = [table.table_id for table in self.tables if table.table_id in set(table_ids) and table.table_id in valid]
        self.settings.simple_selected_table_ids = ordered
        self.settings.simple_table_selection_initialized = True
        self.save_settings_now()
        self.refresh_simple_tables()
        self.update_simple_table_summary()

    def on_simple_table_click(self, event: tk.Event) -> None:
        tree = self.simple_table_tree
        if tree is None or tree.identify_column(event.x) != "#1":
            return
        row = tree.identify_row(event.y)
        if not row:
            return
        tree.selection_set(row)
        self.root.after_idle(self.toggle_simple_table_selection)

    def toggle_simple_table_selection(self) -> None:
        if not self.simple_table_tree:
            return
        ids = list(self.simple_table_tree.selection())
        current = set(self.settings.simple_selected_table_ids)
        for table_id in ids:
            if table_id in current:
                current.remove(table_id)
            else:
                current.add(table_id)
        self.set_simple_selected_tables(list(current))

    def set_all_simple_tables(self, enabled: bool) -> None:
        ids = [table.table_id for table in self.tables if not table.error and table.levels] if enabled else []
        self.set_simple_selected_tables(ids)

    def update_pending_analysis_status(self) -> int:
        try:
            count = analysis_db_diff_count(PENDING_ANALYSIS_DB, CHART_ANALYSIS_DB)
        except Exception:
            count = 0
        if count > 0:
            self.pending_analysis_var.set(f"未追記の解析結果: {count:,}差分")
            if self.analysis_commit_button is not None and not self.analysis_running:
                self.analysis_commit_button.configure(state="normal")
        else:
            self.pending_analysis_var.set("未追記の解析結果: なし")
            if self.analysis_commit_button is not None:
                self.analysis_commit_button.configure(state="disabled")
        return count

    def _prepare_pending_analysis_db(self) -> None:
        PENDING_ANALYSIS_DB.parent.mkdir(parents=True, exist_ok=True)
        if PENDING_ANALYSIS_DB.exists():
            return
        if CHART_ANALYSIS_DB.exists():
            shutil.copy2(CHART_ANALYSIS_DB, PENDING_ANALYSIS_DB)

    def request_apply_changes(self) -> None:
        """Apply settings to custom folders, using only committed analysis rows."""
        if self.simple_filter_editor is not None and not self.simple_filter_editor.commit_all():
            return
        if not self.quick_panel.commit_all():
            return
        self.save_settings_now()
        if self.analysis_running:
            self.pending_apply_after_analysis = True
            self.status_var.set("差分解析の完了とDBへの追記を待っています")
            self.set_quick_status("差分解析の完了とDBへの追記を待っています")
            self.append_log("カスタムフォルダーへの反映を予約しました。解析後にDBへ追記してください")
            return
        if self.update_pending_analysis_status() > 0:
            self.pending_apply_after_analysis = True
            self.status_var.set("未追記の解析結果があります。DBへ追記後に自動で反映します")
            self.set_quick_status("DBへの追記を待っています")
            self.append_log("未追記の解析結果があるため、DBへの追記を待っています")
            return
        self.pending_apply_after_analysis = False
        self.start_generate(True, update_analysis=False, use_simple=True)

    def cancel_analysis(self) -> None:
        if not self.analysis_running:
            return
        self.analysis_cancel_event.set()
        if self.analysis_cancel_button is not None:
            self.analysis_cancel_button.configure(state="disabled")
        self.status_var.set("差分解析の中止を受け付けました")
        self.progress_state_var.set("中止処理中")
        self.append_log("差分解析の中止を受け付けました。安全な区切りで停止します")

    def commit_pending_analysis(self) -> None:
        if self.analysis_running:
            messagebox.showinfo(APP_NAME, "差分解析の完了または中止後にDBへ追記してください", parent=self.root)
            return
        if self.busy:
            messagebox.showinfo(APP_NAME, "別の処理を実行中です", parent=self.root)
            return
        pending_count = self.update_pending_analysis_status()
        if pending_count <= 0:
            messagebox.showinfo(APP_NAME, "DBへ追記する解析結果はありません", parent=self.root)
            return
        self.status_var.set("解析結果をDBへ追記中...")
        self.start_progress_display("DBへ追記")
        if self.analysis_commit_button is not None:
            self.analysis_commit_button.configure(state="disabled")

        def task():
            result = merge_chart_analysis_db(PENDING_ANALYSIS_DB, CHART_ANALYSIS_DB)
            try:
                PENDING_ANALYSIS_DB.unlink(missing_ok=True)
            except OSError:
                pass
            return result

        def done(result: Any, error: Exception | None) -> None:
            self.finish_progress_display(error is None, str(error) if error else "")
            if error:
                self.status_var.set("DBへの追記に失敗しました")
                self.append_log(f"ERROR: DBへの追記に失敗しました: {error}")
                self.update_pending_analysis_status()
                messagebox.showerror(APP_NAME, str(error), parent=self.root)
                return
            self.status_var.set(f"解析結果 {pending_count:,}差分をDBへ追記しました")
            self.append_log(f"DBへ追記: 未追記 {pending_count:,}差分 / DB行 {result['merged']:,}件を更新")
            self.update_pending_analysis_status()
            if self.pending_apply_after_analysis:
                self.pending_apply_after_analysis = False
                self.root.after(100, lambda: self.start_generate(True, update_analysis=False, use_simple=True))

        self.run_worker(task, done)

    def request_quick_filter_sync(self) -> None:
        """Write the three play-time numeric conditions without rebuilding folders."""
        self.save_settings_now()
        self._quick_sync_requested = True
        after_id = getattr(self, "_quick_sync_after_id", None)
        if after_id is not None:
            try:
                self.root.after_cancel(after_id)
            except Exception:
                pass
        self._quick_sync_after_id = self.root.after(250, self._start_quick_filter_sync)

    def _start_quick_filter_sync(self) -> None:
        self._quick_sync_after_id = None
        if not getattr(self, "_quick_sync_requested", False):
            return
        if not self.env:
            self._quick_sync_requested = False
            self.set_quick_status("beatoraja環境を読み込んでください")
            return
        if self.busy or self.analysis_running:
            self._quick_sync_after_id = self.root.after(500, self._start_quick_filter_sync)
            return

        self._quick_sync_requested = False
        runtime = self.runtime_settings(use_simple=True)
        song_db = self.env.song_db

        def task():
            initial_backup = ensure_initial_songdata_backup(song_db, SONGDATA_BACKUP_DIR)
            count = write_filter_conditions(song_db, runtime.presets)
            return count, initial_backup

        def done(result: Any, error: Exception | None) -> None:
            if error:
                self.set_quick_status(f"条件の反映に失敗しました: {error}")
            else:
                count, initial_backup = result
                self.set_quick_status("条件を反映しました。フォルダーを開き直してください")
                if initial_backup:
                    self.append_log(f"songdata.db初回バックアップ: {initial_backup}")
            if getattr(self, "_quick_sync_requested", False):
                self._quick_sync_after_id = self.root.after(100, self._start_quick_filter_sync)

        if not self.run_worker(task, done):
            self._quick_sync_requested = True
            self._quick_sync_after_id = self.root.after(500, self._start_quick_filter_sync)

    def sync_simple_filter_values(self) -> None:
        if self.simple_filter_editor is not None and not self.simple_filter_editor.commit_all():
            return
        self.save_settings_now()
        self.status_var.set("カスタムフォルダーへ反映中...")

        def done(result: Any, error: Exception | None) -> None:
            if error:
                self.status_var.set("カスタムフォルダーへの反映に失敗")
                messagebox.showerror(APP_NAME, str(error), parent=self.root)
            else:
                count, initial_backup = result
                self.status_var.set(f"カスタムフォルダーへ {count:,}件を反映しました")
                if initial_backup:
                    self.append_log(f"songdata.db初回バックアップ: {initial_backup}")

        self.run_worker(lambda: self.sync_filter_values(use_simple=True), done)

    def launch_simple_mode(self) -> None:
        self.save_settings_now()
        try:
            subprocess.Popen([sys.executable, str(SCRIPT_DIR / "launch_app.py"), "--simple"], cwd=str(SCRIPT_DIR))
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"シンプルモードを起動できませんでした。\n{exc}", parent=self.root)
            return
        self.on_close()

    def launch_detailed_mode(self) -> None:
        if self.simple_filter_editor is not None:
            self.simple_filter_editor.commit_all()
        self.save_settings_now()
        try:
            subprocess.Popen([sys.executable, str(SCRIPT_DIR / "launch_app.py"), "--detailed"], cwd=str(SCRIPT_DIR))
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"詳細編集モードを起動できませんでした。\n{exc}", parent=self.root)
            return
        self.on_close()

    def append_log(self, text: str) -> None:
        LOGGER.info(text)
        if self.log_text:
            self.log_text.insert("end", text + "\n")
            self.log_text.see("end")

    def save_settings_now(self) -> None:
        self.settings.rian_root = self.root_var.get().strip()
        self.settings.player_name = self.player_var.get().strip() or "player1"
        self.settings.current_song_history_path = self.history_path_var.get().strip()
        self.settings.instant_density_table_enabled = bool(self.instant_enabled_var.get())
        self.settings.maker_table_enabled = bool(self.maker_enabled_var.get())
        self.settings.random_course_enabled = bool(self.random_course_enabled_var.get())
        self.settings.random_course_filter_enabled = bool(self.random_course_filter_enabled_var.get())
        try:
            self.settings.random_course_stages = max(1, min(20, int(self.random_course_stages_var.get())))
        except (TypeError, ValueError, tk.TclError):
            self.settings.random_course_stages = 4
            self.random_course_stages_var.set(4)
        self.settings.random_course_distinct = bool(self.random_course_distinct_var.get())
        self.settings.random_course_folder_name = self.random_course_folder_name_var.get().strip() or "ランダムコース"
        self.settings.random_course_name = self.random_course_name_var.get().strip() or "ALL RANDOM {count}"
        self.settings.random_course_filter_name = self.random_course_filter_name_var.get().strip() or "{filter} RANDOM {count}"
        try:
            if self.mode == "detailed":
                self.settings.main_geometry = self.root.geometry()
            else:
                self.settings.simple_geometry = self.root.geometry()
        except Exception:
            pass
        focus_set = self.simple_filter_set_var.get().strip()
        if focus_set:
            self.settings.simple_filter_set = focus_set
        save_settings(SETTINGS_PATH, self.settings)

    def browse_history_path(self) -> None:
        path = filedialog.askopenfilename(
            title="history_cursong.jsonを選択",
            filetypes=[("JSON", "*.json"), ("すべて", "*.*")],
            initialdir=str(Path(self.history_path_var.get()).parent) if self.history_path_var.get() else None,
        )
        if path:
            self.history_path_var.set(path)
            self.save_settings_now()
            self.last_history_signature = None

    def browse_root(self) -> None:
        path = filedialog.askdirectory(title="beatoraja本体フォルダーを選択", initialdir=self.root_var.get() or None)
        if path:
            self.root_var.set(path)
            self.refresh_player_choices()

    def refresh_player_choices(self) -> None:
        raw = self.root_var.get().strip()
        if not raw:
            return
        try:
            names = list_player_names(Path(raw))
        except Exception:
            names = [self.player_var.get().strip() or "player1"]
        current = self.player_var.get().strip() or self.settings.player_name or "player1"
        if current not in names:
            current = names[0] if names else "player1"
        if self.player_combo is not None:
            self.player_combo.configure(values=tuple(names))
        self.player_var.set(current)

    def auto_detect_root(self) -> None:
        candidates = common_root_candidates()
        if not candidates:
            messagebox.showinfo(APP_NAME, "自動検出できませんでした。参照ボタンから選択してください。", parent=self.root)
            return
        self.root_var.set(str(candidates[0]))
        self.refresh_player_choices()
        self.load_environment()

    def try_auto_load(self) -> None:
        # 初回起動時は利用者固有の本体パスを推測・入力しない。
        # 保存済みの明示的な設定が有効な場合だけ自動で読み込む。
        raw_path = self.root_var.get().strip()
        if not raw_path:
            return
        path = Path(raw_path)
        if (path / "config_sys.json").exists() or (path / "config.json").exists():
            self.load_environment()

    def load_environment(self) -> None:
        try:
            self.refresh_player_choices()
            self.env = detect_environment(
                Path(self.root_var.get().strip()),
                self.player_var.get().strip() or "player1",
            )
            self.settings.rian_root = str(self.env.root)
            self.settings.player_name = self.env.player_name
            self.root_var.set(str(self.env.root))
            self.player_var.set(self.env.player_name)
            info = summarize_environment(self.env)
            self.env_text_var.set("\n".join(f"{k}: {v}" for k, v in info.items()))
            self.status_var.set(f"{self.env.environment_label}環境を読み込みました")
            self.default_profile_var.set(
                f"表示設定：{self.env.environment_label} / {self.env.root}（この本体フォルダ専用）"
            )
            self.append_log(f"環境読込: {self.env.environment_label} / {self.env.root}")
            imported = sync_default_folder_catalog(self.settings, self.env.default_json)
            if imported:
                self.append_log(f"既存default.json項目を{imported}件取り込み")
            self.refresh_structure()
            self.save_settings_now()
            self.reload_tables(fetch_missing=False)
        except Exception as exc:
            self.env = None
            self.env_text_var.set(str(exc))
            messagebox.showerror(APP_NAME, str(exc), parent=self.root)

    def reload_tables(self, fetch_missing: bool) -> None:
        if not self.env:
            messagebox.showerror(APP_NAME, "先にbeatoraja環境を読み込んでください", parent=self.root)
            return

        def task() -> list[TableInfo]:
            # Re-read config so additions made by rian are reflected.
            fresh = detect_environment(self.env.root, self.player_var.get().strip() or "player1")
            self.env = fresh
            return load_tables(fresh, self.settings.app_tables, CACHE_DIR, fetch_missing=fetch_missing)

        def done(result: Any, error: Exception | None) -> None:
            if error:
                messagebox.showerror(APP_NAME, str(error), parent=self.root)
                self.status_var.set("難易度表の読込に失敗")
                return
            self.tables = result
            self.table_by_id = {t.table_id: t for t in self.tables}
            known = set(self.table_by_id)
            self.settings.base_table_ids = [x for x in self.settings.base_table_ids if x in known]
            self.settings.cross_table_ids = [x for x in self.settings.cross_table_ids if x in known]
            migrated = ensure_table_combinations(self.settings, self.tables)
            if migrated:
                self.append_log(f"掛け合わせ済み難易度表設定を{migrated}件移行・補正")
            simple_changed = initialize_simple_selection(self.settings, self.tables)
            if simple_changed:
                self.append_log("シンプルモードの対象表・フィルター初期値を更新")
            self.refresh_tables()
            self.refresh_structure()
            self.refresh_simple_tables()
            self.refresh_simple_filter_sets()
            self.save_settings_now()
            loaded = sum(1 for t in self.tables if not t.error)
            self.status_var.set(f"難易度表 {loaded}/{len(self.tables)}件を読込")
            self.append_log(f"難易度表読込: 成功 {loaded} / 全体 {len(self.tables)}")

        self.run_worker(task, done)

    def refresh_tables(self) -> None:
        if not self.table_tree:
            return
        self.table_tree.delete(*self.table_tree.get_children())
        active_underlying = set(combination_table_ids(self.settings.table_combinations, visible_only=True))
        for table in self.tables:
            direct_count = sum(
                1 for combo in self.settings.table_combinations if combo.visible
                for ref in combination_input_refs(combo)
                if split_source_ref(ref) == ("table", table.table_id)
            )
            roles = []
            if direct_count:
                roles.append(f"直接入力{direct_count}")
            elif table.table_id in active_underlying:
                roles.append("派生入力")
            status = "OK" if not table.error else table.error.splitlines()[0][:25]
            scale_entry = match_difficulty_scale(table, self.difficulty_registry)
            scale_name = str(scale_entry.get("name")) if scale_entry else "未登録"
            self.table_tree.insert(
                "",
                "end",
                iid=table.table_id,
                values=(
                    "/".join(roles) or "—",
                    table.source,
                    status,
                    scale_name,
                    len(table.levels),
                    table.chart_count,
                    table.name,
                    table.url,
                ),
            )

    def selected_table_ids(self) -> list[str]:
        return list(self.table_tree.selection()) if self.table_tree else []

    def toggle_base_tables(self) -> None:
        for table_id in self.selected_table_ids():
            if table_id in self.settings.base_table_ids:
                self.settings.base_table_ids.remove(table_id)
            else:
                self.settings.base_table_ids.append(table_id)
        self.save_settings_now()
        self.refresh_tables()

    def toggle_cross_tables(self) -> None:
        for table_id in self.selected_table_ids():
            if table_id in self.settings.cross_table_ids:
                self.settings.cross_table_ids.remove(table_id)
            else:
                self.settings.cross_table_ids.append(table_id)
        self.save_settings_now()
        self.refresh_tables()

    def add_table(self) -> None:
        if not self.env:
            messagebox.showerror(APP_NAME, "先にbeatoraja環境を読み込んでください", parent=self.root)
            return
        dialog = AddTableDialog(self.root)
        self.root.wait_window(dialog)
        rec = dialog.result
        if not rec:
            return
        if any(t.url == rec.url for t in self.settings.app_tables):
            messagebox.showinfo(APP_NAME, "ツールには既に登録されています", parent=self.root)
            return
        if rec.register_to_beatoraja:
            if is_rian_probably_running():
                proceed = messagebox.askyesno(
                    APP_NAME,
                    "beatorajaが起動している可能性があります。\n"
                    "起動中にconfig_sys.jsonを書き換えると、終了時に上書きされる可能性があります。\n\n"
                    "それでも登録しますか？",
                    parent=self.root,
                )
                if not proceed:
                    return
            try:
                added, backup = register_table_url(self.env.config, rec.url)
                if added:
                    self.append_log(f"beatorajaへURL登録: {rec.url}")
                    if backup:
                        self.append_log(f"設定バックアップ: {backup}")
            except Exception as exc:
                messagebox.showerror(APP_NAME, str(exc), parent=self.root)
                return
        self.settings.app_tables.append(rec)
        self.save_settings_now()
        self.status_var.set("難易度表を取得中...")

        def task() -> TableInfo:
            table = parse_remote_table(rec.url, rec.header_url)
            save_tool_cache(CACHE_DIR, table)
            return table

        def done(result: Any, error: Exception | None) -> None:
            if error:
                messagebox.showwarning(
                    APP_NAME,
                    "URLは登録しましたが、表データの取得に失敗しました。\n"
                    "header JSON URLを指定して再登録するか、beatoraja側で取得後に再読込してください。\n\n"
                    f"{error}",
                    parent=self.root,
                )
            else:
                self.append_log(f"ツール表取得: {result.name} ({result.chart_count}譜面)")
            self.reload_tables(fetch_missing=False)

        self.run_worker(task, done)

    def remove_app_table(self) -> None:
        ids = set(self.selected_table_ids())
        if not ids:
            return
        app_urls = {t.url for t in self.settings.app_tables}
        targets = [t for t in self.tables if t.table_id in ids and t.url in app_urls]
        if not targets:
            messagebox.showinfo(APP_NAME, "選択した表はツール独自登録ではありません", parent=self.root)
            return
        names = "\n".join(t.name for t in targets)
        if not messagebox.askyesno(APP_NAME, f"ツール独自登録から削除しますか？\n\n{names}\n\nbeatoraja登録は削除しません。", parent=self.root):
            return
        urls = {t.url for t in targets}
        self.settings.app_tables = [x for x in self.settings.app_tables if x.url not in urls]
        self.settings.base_table_ids = [x for x in self.settings.base_table_ids if x not in ids]
        self.settings.cross_table_ids = [x for x in self.settings.cross_table_ids if x not in ids]
        removed_combo_ids: set[str] = set()
        changed = True
        while changed:
            changed = False
            for combo in self.settings.table_combinations:
                if combo.combination_id in removed_combo_ids:
                    continue
                for ref in combination_input_refs(combo):
                    kind, value = split_source_ref(ref)
                    if (kind == "table" and value in ids) or (kind == "combo" and value in removed_combo_ids):
                        removed_combo_ids.add(combo.combination_id)
                        changed = True
                        break
        self.settings.table_combinations = [
            combo for combo in self.settings.table_combinations
            if combo.combination_id not in removed_combo_ids
        ]
        self.save_settings_now()
        self.reload_tables(fetch_missing=False)

    def table_display_name(self, table_id: str) -> str:
        if not table_id:
            return "—"
        table = self.table_by_id.get(table_id)
        return table.name if table else f"未取得: {table_id[:12]}"

    def source_display_name(self, ref: str) -> str:
        kind, value = split_source_ref(ref)
        if kind == "table":
            return self.table_display_name(value)
        combo = next((item for item in self.settings.table_combinations if item.combination_id == value), None)
        return combo.name if combo else f"削除済み生成表: {value[:12]}"

    def combination_input_summary(self, combo: TableCombination) -> str:
        names = [self.source_display_name(ref) for ref in combination_input_refs(combo)]
        return " → ".join(names) if names else "入力なし"

    def refresh_structure(self) -> None:
        if self.combination_tree:
            selected = set(self.combination_tree.selection())
            self.combination_tree.delete(*self.combination_tree.get_children())
            for combo in self.settings.table_combinations:
                self.combination_tree.insert(
                    "", "end", iid=combo.combination_id,
                    values=(
                        "ON" if combo.visible else "OFF",
                        combo.name,
                        combination_method_label(combo.method),
                        self.combination_input_summary(combo),
                    ),
                )
            restore = [iid for iid in selected if self.combination_tree.exists(iid)]
            if restore:
                self.combination_tree.selection_set(restore)
        if self.default_folder_tree:
            selected = set(self.default_folder_tree.selection())
            self.default_folder_tree.delete(*self.default_folder_tree.get_children())
            for entry in active_default_folders(self.settings, self.env.root if self.env else None):
                payload = entry.data or {}
                kind = "階層フォルダ" if payload.get("folder") else ("SQLフォルダ" if payload.get("sql") else "その他")
                self.default_folder_tree.insert(
                    "", "end", iid=entry.entry_id,
                    values=("ON" if entry.visible else "OFF", entry.name, kind),
                )
            restore = [iid for iid in selected if self.default_folder_tree.exists(iid)]
            if restore:
                self.default_folder_tree.selection_set(restore)

    def selected_combination_indices(self) -> list[int]:
        if not self.combination_tree:
            return []
        ids = set(self.combination_tree.selection())
        return [index for index, combo in enumerate(self.settings.table_combinations) if combo.combination_id in ids]

    def add_combination(self) -> None:
        if not self.tables:
            messagebox.showerror(APP_NAME, "先に難易度表を読み込んでください", parent=self.root)
            return
        dialog = CombinationDialog(self.root, self.tables, self.settings.table_combinations)
        self.root.wait_window(dialog)
        if dialog.result:
            if any(combo.name == dialog.result.name for combo in self.settings.table_combinations):
                messagebox.showerror(APP_NAME, "同じフォルダ名が既にあります", parent=self.root)
                return
            try:
                validate_combination_graph([*self.settings.table_combinations, dialog.result])
            except ToolError as exc:
                messagebox.showerror(APP_NAME, str(exc), parent=self.root)
                return
            self.settings.table_combinations.append(dialog.result)
            self.save_settings_now()
            self.refresh_structure()
            self.refresh_tables()
            self.combination_tree.selection_set(dialog.result.combination_id)

    def edit_combination(self) -> None:
        indices = self.selected_combination_indices()
        if len(indices) != 1:
            if indices:
                messagebox.showinfo(APP_NAME, "編集する項目を1件だけ選択してください", parent=self.root)
            return
        index = indices[0]
        source = self.settings.table_combinations[index]
        dialog = CombinationDialog(self.root, self.tables, self.settings.table_combinations, source)
        self.root.wait_window(dialog)
        if not dialog.result:
            return
        if any(i != index and combo.name == dialog.result.name for i, combo in enumerate(self.settings.table_combinations)):
            messagebox.showerror(APP_NAME, "同じフォルダ名が既にあります", parent=self.root)
            return
        candidate = list(self.settings.table_combinations)
        candidate[index] = dialog.result
        try:
            validate_combination_graph(candidate)
        except ToolError as exc:
            messagebox.showerror(APP_NAME, str(exc), parent=self.root)
            return
        old_name = source.name
        self.settings.table_combinations[index] = dialog.result
        if old_name and old_name not in self.settings.managed_folder_names:
            self.settings.managed_folder_names.append(old_name)
        self.save_settings_now()
        self.refresh_structure()
        self.refresh_tables()
        self.combination_tree.selection_set(dialog.result.combination_id)

    def copy_combination(self) -> None:
        indices = self.selected_combination_indices()
        if len(indices) != 1:
            return
        source = self.settings.table_combinations[indices[0]]
        name = source.name + " のコピー"
        used = {combo.name for combo in self.settings.table_combinations}
        serial = 2
        while name in used:
            name = f"{source.name} のコピー{serial}"
            serial += 1
        copied = TableCombination(
            name=name, visible=source.visible,
            input_refs=list(combination_input_refs(source)),
            method=source.method,
        )
        self.settings.table_combinations.insert(indices[0] + 1, copied)
        self.save_settings_now()
        self.refresh_structure()
        self.refresh_tables()
        self.combination_tree.selection_set(copied.combination_id)

    def delete_combinations(self) -> None:
        indices = self.selected_combination_indices()
        if not indices:
            return
        selected_ids = {self.settings.table_combinations[i].combination_id for i in indices}
        remove_ids = set(selected_ids)
        changed = True
        while changed:
            changed = False
            for combo in self.settings.table_combinations:
                if combo.combination_id in remove_ids:
                    continue
                if any(
                    split_source_ref(ref)[0] == "combo" and split_source_ref(ref)[1] in remove_ids
                    for ref in combination_input_refs(combo)
                ):
                    remove_ids.add(combo.combination_id)
                    changed = True
        direct_names = [combo.name for combo in self.settings.table_combinations if combo.combination_id in selected_ids]
        dependent_names = [
            combo.name for combo in self.settings.table_combinations
            if combo.combination_id in remove_ids and combo.combination_id not in selected_ids
        ]
        detail = "\n".join(f"・{name}" for name in direct_names[:12])
        if dependent_names:
            detail += "\n\n依存しているため同時に削除：\n" + "\n".join(
                f"・{name}" for name in dependent_names[:12]
            )
            if len(dependent_names) > 12:
                detail += f"\n・ほか{len(dependent_names) - 12}件"
        if not messagebox.askyesno(
            APP_NAME,
            f"掛け合わせ済み難易度表を削除しますか？\n\n{detail}",
            parent=self.root,
        ):
            return
        kept: list[TableCombination] = []
        for combo in self.settings.table_combinations:
            if combo.combination_id in remove_ids:
                if combo.name and combo.name not in self.settings.managed_folder_names:
                    self.settings.managed_folder_names.append(combo.name)
            else:
                kept.append(combo)
        self.settings.table_combinations = kept
        self.save_settings_now()
        self.refresh_structure()
        self.refresh_tables()

    def move_combination(self, delta: int) -> None:
        indices = self.selected_combination_indices()
        if len(indices) != 1:
            return
        index = indices[0]
        target = index + delta
        if not (0 <= target < len(self.settings.table_combinations)):
            return
        self.settings.table_combinations[index], self.settings.table_combinations[target] = (
            self.settings.table_combinations[target], self.settings.table_combinations[index]
        )
        self.save_settings_now()
        self.refresh_structure()
        self.combination_tree.selection_set(self.settings.table_combinations[target].combination_id)

    def set_combination_visibility(self, visible: bool) -> None:
        indices = self.selected_combination_indices()
        for index in indices:
            self.settings.table_combinations[index].visible = visible
        if indices:
            self.save_settings_now()
            self.refresh_structure()
            self.refresh_tables()

    def reload_default_folder_catalog(self) -> None:
        if not self.env:
            messagebox.showerror(APP_NAME, "先にbeatoraja環境を読み込んでください", parent=self.root)
            return
        try:
            imported = sync_default_folder_catalog(self.settings, self.env.default_json)
            self.save_settings_now()
            self.refresh_structure()
            self.status_var.set(f"default.jsonを再読込しました（新規{imported}件）")
        except Exception as exc:
            messagebox.showerror(APP_NAME, str(exc), parent=self.root)

    def selected_default_folder_ids(self) -> set[str]:
        return set(self.default_folder_tree.selection()) if self.default_folder_tree else set()

    def set_default_folder_visibility(self, visible: bool) -> None:
        ids = self.selected_default_folder_ids()
        folders = active_default_folders(self.settings, self.env.root if self.env else None)
        for entry in folders:
            if entry.entry_id in ids:
                entry.visible = visible
        if ids:
            self.save_settings_now()
            self.refresh_structure()

    def refresh_presets(self, refresh_quick: bool = True) -> None:
        if self.preset_tree:
            selected_ids = {
                self.settings.presets[int(iid)].preset_id
                for iid in self.preset_tree.selection()
                if str(iid).isdigit() and int(iid) < len(self.settings.presets)
            }
            self.preset_tree.delete(*self.preset_tree.get_children())
            restore: list[str] = []
            for index, preset in enumerate(self.settings.presets):
                enabled_count = sum(1 for c in preset.conditions if c.enabled)
                self.preset_tree.insert(
                    "",
                    "end",
                    iid=str(index),
                    values=("✓" if preset.visible else "—", preset.group_name or "—", preset.join, enabled_count, preset.name),
                )
                if preset.preset_id in selected_ids:
                    restore.append(str(index))
            if restore:
                self.preset_tree.selection_set(*restore)
            self.update_preset_summary()
        if self.simple_filter_editor is not None:
            self.simple_filter_editor.rebuild(self.ensure_simple_filter_edit_set())
        if refresh_quick:
            self.quick_panel.refresh_presets()

    def selected_preset_indices(self) -> list[int]:
        if not self.preset_tree:
            return []
        return sorted(
            int(iid) for iid in self.preset_tree.selection()
            if str(iid).isdigit() and 0 <= int(iid) < len(self.settings.presets)
        )

    def selected_preset_index(self) -> int | None:
        selected = self.selected_preset_indices()
        return selected[0] if len(selected) == 1 else None

    def update_preset_summary(self) -> None:
        total = len(self.settings.presets)
        visible = sum(1 for p in self.settings.presets if p.visible)
        selected = len(self.selected_preset_indices())
        self.preset_summary_var.set(f"全{total}件 / 表示{visible}件 / 選択{selected}件")

    def select_all_presets(self, _event: tk.Event | None = None) -> str:
        if self.preset_tree:
            self.preset_tree.selection_set(*self.preset_tree.get_children())
            self.update_preset_summary()
        return "break"

    def select_legacy_bpm_presets(self) -> None:
        if not self.preset_tree:
            return
        targets = [
            str(index) for index, preset in enumerate(self.settings.presets)
            if str(preset.preset_id or "").startswith("builtin:rhythm-")
        ]
        current = self.preset_tree.selection()
        if current:
            self.preset_tree.selection_remove(*current)
        if targets:
            self.preset_tree.selection_set(*targets)
            self.preset_tree.see(targets[0])
            self.status_var.set(f"v0.6.0の詳細BPMテンプレートを{len(targets)}件選択しました")
        else:
            self.status_var.set("詳細BPMテンプレートはありません")
        self.update_preset_summary()

    def set_selected_presets_visible(self, visible: bool) -> None:
        indices = self.selected_preset_indices()
        if not indices:
            return
        for index in indices:
            preset = self.settings.presets[index]
            preset.visible = visible
            self.settings.simple_preset_enabled[preset.preset_id] = visible
        self.save_settings_now()
        self.refresh_presets()
        self.status_var.set(f"選択した{len(indices)}件を{'表示' if visible else '非表示'}にしました")

    def add_preset(self) -> None:
        dialog = PresetDialog(self.root)
        self.root.wait_window(dialog)
        if dialog.result:
            self.settings.presets.append(dialog.result)
            self.settings.simple_preset_enabled[dialog.result.preset_id] = bool(dialog.result.visible)
            self.save_settings_now()
            self.refresh_presets()

    def add_initial_templates(self) -> None:
        merged, added = merge_initial_filter_presets(self.settings.presets)
        self.settings.presets = merged
        if added:
            self.save_settings_now()
            self.refresh_presets()
            messagebox.showinfo(
                APP_NAME,
                f"不足していた初期テンプレートを{added}件追加しました。\n"
                "JSON＋構成DBを生成し、beatorajaを再起動すると階層が反映されます。",
                parent=self.root,
            )
        else:
            messagebox.showinfo(APP_NAME, "初期テンプレートはすべて登録済みです。", parent=self.root)

    def add_rhythm_bpm_bulk(self) -> None:
        dialog = RhythmBpmBulkDialog(self.root)
        self.root.wait_window(dialog)
        if not dialog.result:
            return
        existing = {(p.group_name.strip(), p.name.strip()) for p in self.settings.presets}
        additions = [p for p in dialog.result if (p.group_name.strip(), p.name.strip()) not in existing]
        skipped = len(dialog.result) - len(additions)
        if additions:
            self.settings.presets.extend(additions)
            self.save_settings_now()
            self.refresh_presets()
        detail = f"{len(additions)}件を追加しました。"
        if skipped:
            detail += f" 同じ親グループ・名称の{skipped}件は追加しませんでした。"
        detail += "\nJSON＋構成DBを生成し、beatorajaを再起動すると階層が反映されます。"
        messagebox.showinfo(APP_NAME, detail, parent=self.root)

    def edit_preset(self) -> None:
        index = self.selected_preset_index()
        if index is None:
            return
        dialog = PresetDialog(self.root, self.settings.presets[index])
        self.root.wait_window(dialog)
        if dialog.result:
            self.settings.presets[index] = dialog.result
            self.settings.simple_preset_enabled[dialog.result.preset_id] = bool(dialog.result.visible)
            self.save_settings_now()
            self.refresh_presets()
            self.preset_tree.selection_set(str(index))

    def copy_preset(self) -> None:
        index = self.selected_preset_index()
        if index is None:
            return
        src = self.settings.presets[index]
        copied = Preset(
            name=src.name + " のコピー",
            visible=src.visible,
            join=src.join,
            conditions=[Condition(c.field, c.op, c.value, c.enabled) for c in src.conditions],
            group_name=src.group_name,
        )
        self.settings.presets.insert(index + 1, copied)
        self.settings.simple_preset_enabled[copied.preset_id] = bool(copied.visible)
        self.save_settings_now()
        self.refresh_presets()
        self.preset_tree.selection_set(str(index + 1))

    def delete_preset(self) -> None:
        indices = self.selected_preset_indices()
        if not indices:
            return
        targets = [self.settings.presets[index] for index in indices]
        if len(targets) == 1:
            detail = targets[0].name
        else:
            names = [f"・{preset.group_name + ' / ' if preset.group_name else ''}{preset.name}" for preset in targets[:12]]
            if len(targets) > 12:
                names.append(f"・ほか{len(targets) - 12}件")
            detail = f"{len(targets)}件をまとめて削除します。\n\n" + "\n".join(names)
        if not messagebox.askyesno(APP_NAME, f"削除しますか？\n\n{detail}", parent=self.root):
            return
        next_index = min(indices[0], max(0, len(self.settings.presets) - len(indices) - 1))
        for index in reversed(indices):
            preset_id = self.settings.presets[index].preset_id
            self.settings.simple_preset_enabled.pop(preset_id, None)
            del self.settings.presets[index]
        self.save_settings_now()
        self.refresh_presets()
        if self.preset_tree and self.settings.presets:
            self.preset_tree.selection_set(str(next_index))
            self.preset_tree.see(str(next_index))
        self.update_preset_summary()
        self.status_var.set(f"フィルターを{len(indices)}件削除しました")

    def move_preset(self, delta: int) -> None:
        index = self.selected_preset_index()
        if index is None:
            return
        new_index = index + delta
        if not (0 <= new_index < len(self.settings.presets)):
            return
        self.settings.presets[index], self.settings.presets[new_index] = (
            self.settings.presets[new_index],
            self.settings.presets[index],
        )
        self.save_settings_now()
        self.refresh_presets()
        self.preset_tree.selection_set(str(new_index))

    def runtime_settings(self, use_simple: bool | None = None) -> AppSettings:
        simple = self.mode == "simple" if use_simple is None else bool(use_simple)
        if simple:
            initialize_simple_selection(self.settings, self.tables)
            return build_simple_runtime_settings(self.settings, self.tables)
        return self.settings

    def ensure_ready(self, use_simple: bool | None = None) -> AppSettings:
        self.save_settings_now()
        if not self.env:
            raise ToolError("beatoraja環境が未読込です")
        if not self.tables:
            raise ToolError("難易度表が未読込です")
        runtime = self.runtime_settings(use_simple)
        validate_combination_graph(runtime.table_combinations)
        visible_combinations = [combo for combo in runtime.table_combinations if combo.visible]
        if not visible_combinations and not runtime.instant_density_table_enabled:
            if (self.mode == "simple" if use_simple is None else bool(use_simple)):
                raise ToolError("対象難易度表を1件以上選択してください")
            raise ToolError("表示する掛け合わせ済み難易度表を1件以上設定してください")
        if not any(preset.visible for preset in runtime.presets):
            raise ToolError("使用するフィルターを1件以上選択してください")
        names = [combo.name.strip() for combo in visible_combinations]
        if any(not name for name in names):
            raise ToolError("フォルダ名が空の難易度表設定があります")
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise ToolError("同じフォルダ名が重複しています: " + ", ".join(duplicates))
        if runtime.instant_density_table_enabled and "表外差分即席難易度表" in names:
            raise ToolError("『表外差分即席難易度表』は追加探索フォルダ名と重複するため変更してください")
        generated_names = set(names)
        if runtime.instant_density_table_enabled:
            generated_names.add("表外差分即席難易度表")
        collisions = sorted(
            generated_names & {entry.name for entry in active_default_folders(runtime, self.env.root) if entry.visible}
        )
        if collisions:
            raise ToolError("既存default.json項目とフォルダ名が重複しています: " + ", ".join(collisions))
        available = {t.table_id for t in self.tables if not t.error and t.levels}
        combo_map = {combo.combination_id: combo for combo in runtime.table_combinations}
        visited: set[str] = set()
        missing_tables: set[str] = set()

        def check_combo(combo: TableCombination) -> None:
            if combo.combination_id in visited:
                return
            visited.add(combo.combination_id)
            refs = combination_input_refs(combo)
            required = 1 if combo.method == "plain" else 2
            if len(refs) < required:
                raise ToolError(
                    f"『{combo.name}』は『{combination_method_label(combo.method)}』に必要な入力数を満たしていません"
                )
            for ref in refs:
                kind, value = split_source_ref(ref)
                if kind == "table":
                    if value not in available:
                        missing_tables.add(value)
                else:
                    dependency = combo_map.get(value)
                    if dependency is None:
                        raise ToolError(f"『{combo.name}』が存在しない生成済み表を参照しています")
                    check_combo(dependency)

        for combo in visible_combinations:
            check_combo(combo)
        if missing_tables:
            raise ToolError(
                "生成に使用する表の一部が未取得です。難易度表を再読込してください。\n"
                + "\n".join(sorted(missing_tables))
            )
        return runtime

    def preview(self, use_simple: bool | None = None) -> None:
        try:
            runtime = self.ensure_ready(use_simple)
        except Exception as exc:
            messagebox.showerror(APP_NAME, str(exc), parent=self.root)
            return
        self.preview_var.set("計算中...")

        def task():
            return preview_counts(self.env, self.tables, runtime, CHART_ANALYSIS_DB)  # type: ignore[arg-type]

        def done(result: Any, error: Exception | None) -> None:
            if error:
                self.preview_var.set("計算失敗")
                messagebox.showerror(APP_NAME, str(error), parent=self.root)
                return
            view_count, chart_total, _missing_same = result
            self.preview_var.set(
                f"末端フォルダ {view_count:,}個 / 各フォルダ所属の延べ譜面 {chart_total:,}件"
            )

        self.run_worker(task, done)

    def generate(self, write_json: bool, update_analysis: bool = False, use_simple: bool | None = None):
        runtime = self.ensure_ready(use_simple)
        initial_backup = ensure_initial_songdata_backup(self.env.song_db, SONGDATA_BACKUP_DIR)  # type: ignore[union-attr]
        result = generate_all(
            self.env, self.tables, runtime, write_json=write_json,
            analysis_db=CHART_ANALYSIS_DB, update_analysis=update_analysis,
            progress_callback=self.enqueue_progress,
        )  # type: ignore[arg-type]
        result.songdata_initial_backup_path = initial_backup
        return result

    def sync_filter_values(self, use_simple: bool | None = None) -> tuple[int, Path | None]:
        if not self.env:
            raise ToolError("beatoraja環境が未読込です")
        self.save_settings_now()
        runtime = self.runtime_settings(use_simple)
        initial_backup = ensure_initial_songdata_backup(self.env.song_db, SONGDATA_BACKUP_DIR)
        return write_filter_conditions(self.env.song_db, runtime.presets), initial_backup

    def start_generate(
        self,
        write_json: bool,
        use_simple: bool | None = None,
        *,
        update_analysis: bool = False,
    ) -> None:
        simple = self.mode == "simple" if use_simple is None else bool(use_simple)
        if simple:
            if self.simple_filter_editor is not None and not self.simple_filter_editor.commit_all():
                return
            if not self.quick_panel.commit_all():
                return
        if self.busy:
            messagebox.showinfo(APP_NAME, "別の処理を実行中です", parent=self.root)
            return
        try:
            self.ensure_ready(use_simple)
        except Exception as exc:
            messagebox.showerror(APP_NAME, str(exc), parent=self.root)
            return
        if write_json and is_rian_probably_running():
            if not messagebox.askyesno(
                APP_NAME,
                "beatorajaが起動している可能性があります。\n"
                "default.jsonの変更は起動中には反映されず、次回起動から有効です。\n\n"
                "反映を続けますか？",
                parent=self.root,
            ):
                return
        self.set_quick_status("カスタムフォルダーへ反映中...")
        task_name = "カスタムフォルダー編集" if write_json else "フィルター条件更新"
        self.start_progress_display(task_name)
        self.append_log("カスタムフォルダーへの反映を開始しました")

        def done(result: Any, error: Exception | None) -> None:
            self.finish_progress_display(error is None, str(error) if error else "")
            if error:
                self.set_quick_status("カスタムフォルダーへの反映に失敗しました")
                self.append_log(f"ERROR: {error}")
                messagebox.showerror(APP_NAME, str(error), parent=self.root)
                return
            self.save_settings_now()
            self.refresh_structure()
            self.set_quick_status("カスタムフォルダーへ反映しました")
            self.append_log(
                f"反映完了: 末端フォルダー {result.view_count:,} / "
                f"空のフォルダー {result.empty_view_count:,}"
            )
            LOGGER.info(
                "generation detail: membership=%s conditions=%s analysis_rows=%s",
                result.membership_count,
                result.condition_count,
                result.analysis_row_count,
            )
            if result.analysis_reclassified:
                self.append_log(f"譜面傾向の判定更新: {result.analysis_reclassified:,}譜面")
            if result.analysis_analyzed or result.analysis_missing or result.analysis_failed:
                self.append_log(
                    f"譜面解析: 新規/更新 {result.analysis_analyzed:,} / キャッシュ {result.analysis_cached:,} / "
                    f"ファイルなし {result.analysis_missing:,} / 失敗 {result.analysis_failed:,}"
                )
            if result.instant_chart_count:
                self.append_log(f"表外差分即席難易度表: {result.instant_chart_count:,}譜面")
            if result.maker_folder_count or result.maker_chart_count:
                self.append_log(
                    f"差分制作指標: {result.maker_folder_count:,}フォルダ / 対象譜面 {result.maker_chart_count:,} / "
                    f"新規解析 {result.material_analyzed:,} / キャッシュ {result.material_cached:,} "
                    f"(軽量判定 {result.material_quick_cached:,}) / 全音源走査 {result.material_full_scans:,} / "
                    f"失敗 {result.material_failed:,}"
                )
            if result.songdata_initial_backup_path:
                self.append_log(f"songdata.db初回バックアップ: {result.songdata_initial_backup_path}")
            if result.backup_path:
                self.append_log(f"default.jsonバックアップ: {result.backup_path}")
            if write_json:
                ok, detail = validate_default_json(result.folder_json_path)
                self.append_log(f"default.json検証: {'OK' if ok else 'NG'} - {detail}")
            message = (
                "カスタムフォルダーの編集が完了しました。\n\n"
                f"末端フォルダー: {result.view_count:,}\n"
                f"空のフォルダー: {result.empty_view_count:,}"
            )
            if write_json:
                message += "\n\nbeatorajaを再起動すると反映されます。"
            else:
                message += "\n\nbeatoraja側で対象フォルダーを閉じて開き直してください。"
            messagebox.showinfo(APP_NAME, message, parent=self.root)

        self.run_worker(
            lambda: self.generate(write_json, update_analysis=update_analysis, use_simple=use_simple), done
        )


    def start_analysis(
        self,
        force: bool = False,
        use_simple: bool | None = None,
        *,
        automatic: bool = False,
    ) -> None:
        if self.analysis_running:
            if not automatic:
                messagebox.showinfo(APP_NAME, "差分解析はすでに実行中です", parent=self.root)
            return
        if self.busy:
            if not automatic:
                messagebox.showinfo(APP_NAME, "別の処理を実行中です", parent=self.root)
            return
        if not self.env:
            if not automatic:
                messagebox.showerror(APP_NAME, "先に本体フォルダーを読み込んでください", parent=self.root)
            return
        if not self.tables:
            if not automatic:
                messagebox.showerror(APP_NAME, "難易度表がまだ読み込まれていません", parent=self.root)
            return
        self.save_settings_now()
        runtime = self.runtime_settings(True)
        if not runtime.table_combinations:
            if not automatic:
                messagebox.showerror(APP_NAME, "対象難易度表を1件以上ONにしてください", parent=self.root)
            return

        try:
            self._prepare_pending_analysis_db()
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"解析用の一時DBを準備できませんでした。\n{exc}", parent=self.root)
            return

        self.analysis_running = True
        self.analysis_cancel_event.clear()
        if self.analysis_start_button is not None:
            self.analysis_start_button.configure(state="disabled")
        if self.analysis_cancel_button is not None:
            self.analysis_cancel_button.configure(state="normal")
        if self.analysis_commit_button is not None:
            self.analysis_commit_button.configure(state="disabled")
        self.status_var.set("対象難易度表の差分を解析中...")
        self.start_progress_display("差分解析")
        self.append_log("差分解析を開始しました。対象はONになっている難易度表のみです")

        def task():
            return analyze_selected_tables(
                self.env,  # type: ignore[arg-type]
                self.tables,
                runtime,
                PENDING_ANALYSIS_DB,
                force=force,
                progress_callback=self.enqueue_progress,
                cancel_check=self.analysis_cancel_event.is_set,
            )

        def done(result: Any, error: Exception | None) -> None:
            self.analysis_running = False
            if self.analysis_start_button is not None:
                self.analysis_start_button.configure(state="normal")
            if self.analysis_cancel_button is not None:
                self.analysis_cancel_button.configure(state="disabled")

            cancelled = isinstance(error, AnalysisCancelled)
            if cancelled:
                self.finish_progress_display(False, "差分解析を中止しました")
                self.progress_state_var.set("中止")
                self.progress_stage_var.set("差分解析を中止しました")
                self.progress_detail_var.set("中止までに完了した結果は一時保存されています。DBへ追記できます")
                self.status_var.set("差分解析を中止しました。途中までの結果をDBへ追記できます")
                self.append_log("差分解析を中止しました。完了済みの結果は一時保存されています")
            elif error:
                self.finish_progress_display(False, str(error))
                self.status_var.set("差分解析に失敗しました")
                self.append_log(f"ERROR: {error}")
                if not automatic:
                    messagebox.showerror(APP_NAME, str(error), parent=self.root)
            else:
                self.finish_progress_display(True, "")
                self.status_var.set("差分解析が完了しました。DBへ追記してください")
                self.append_log(
                    f"差分解析完了: 対象 {result['target']:,} / 新規・更新 {result['analyzed']:,} / "
                    f"キャッシュ {result['cached']:,} / ファイルなし {result['missing']:,} / 失敗 {result['failed']:,}"
                )
                if not automatic:
                    messagebox.showinfo(
                        APP_NAME,
                        f"対象: {result['target']:,}\n"
                        f"新規・更新: {result['analyzed']:,}\n"
                        f"キャッシュ利用: {result['cached']:,}\n"
                        f"ファイルなし: {result['missing']:,}\n"
                        f"解析失敗: {result['failed']:,}\n\n"
                        "解析結果は一時保存されています。［DBへ追記］で確定してください。",
                        parent=self.root,
                    )

            pending_count = self.update_pending_analysis_status()
            if self.pending_apply_after_analysis:
                if pending_count > 0:
                    self.status_var.set("DBへ追記後にカスタムフォルダーへ自動反映します")
                    self.set_quick_status("DBへの追記を待っています")
                    self.append_log("カスタムフォルダーへの反映予約はDBへの追記を待っています")
                elif error is None or cancelled:
                    # No staged changes remain. A normal completion or user-requested
                    # cancellation can therefore continue with the committed DB.
                    self.pending_apply_after_analysis = False
                    self.root.after(100, lambda: self.start_generate(True, update_analysis=False, use_simple=True))
                else:
                    self.pending_apply_after_analysis = False
                    self.append_log("差分解析に失敗したため、カスタムフォルダーへの反映予約を解除しました")

        started = self.run_worker(task, done)
        if not started:
            self.analysis_running = False
            if self.analysis_start_button is not None:
                self.analysis_start_button.configure(state="normal")
            if self.analysis_cancel_button is not None:
                self.analysis_cancel_button.configure(state="disabled")
            self.update_pending_analysis_status()

    def toggle_maker_diagnostic_mode(self, enabled: bool) -> None:
        self.settings.maker_diagnostic_enabled = bool(enabled)
        if hasattr(self.quick_panel, "maker_diag_var"):
            self.quick_panel.maker_diag_var.set(bool(enabled))
        if not enabled:
            self.hide_maker_diagnostic(suppress=False)
        else:
            self.diagnostic_suppressed_key = ""
            self.last_history_signature = None
        self.save_settings_now()

    def hide_maker_diagnostic(self, suppress: bool = False) -> None:
        if suppress and self.current_material_key:
            self.diagnostic_suppressed_key = self.current_material_key
        if self.maker_diagnostic is not None:
            self.maker_diagnostic.withdraw()

    def current_song_history_candidates(self) -> list[Path]:
        values: list[Path] = []
        configured = self.history_path_var.get().strip()
        if configured:
            values.append(Path(configured))
        values.extend([SCRIPT_DIR / "history_cursong.json", Path.cwd() / "history_cursong.json"])
        if self.env:
            values.extend([self.env.root / "history_cursong.json", self.env.root.parent / "history_cursong.json"])
        result: list[Path] = []
        seen: set[str] = set()
        for path in values:
            key = os.path.normcase(os.path.abspath(str(path)))
            if key not in seen:
                seen.add(key)
                result.append(path)
        return result

    def _history_context_is_maker(self, data: dict[str, Any]) -> bool:
        context_fields = ("folder", "folderName", "folder_path", "category", "barTitle", "selectedFolder")
        context = " ".join(str(data.get(key) or "") for key in context_fields)
        if "差分制作者向け" in context or "差分制作" in context:
            return True
        return bool(self.settings.maker_diagnostic_enabled)

    def poll_current_song(self) -> None:
        try:
            if self.mode == "simple" or not self.env or not self.settings.maker_table_enabled:
                self.hide_maker_diagnostic(False)
                return
            history_path = next((path for path in self.current_song_history_candidates() if path.exists()), None)
            if history_path is None:
                self.hide_maker_diagnostic(False)
                return
            stat = history_path.stat()
            signature = (os.path.normcase(os.path.abspath(str(history_path))), int(stat.st_mtime_ns))
            if signature == self.last_history_signature:
                return
            self.last_history_signature = signature
            data = json.loads(history_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or str(data.get("scene") or "") != "select" or not self._history_context_is_maker(data):
                self.hide_maker_diagnostic(False)
                return
            sha = str(data.get("sha256") or "").lower()
            md5 = str(data.get("md5") or "").lower()
            key = sha or md5
            if not key:
                self.hide_maker_diagnostic(False)
                return
            if key != self.current_material_key:
                self.diagnostic_suppressed_key = ""
            self.current_material_sha, self.current_material_md5, self.current_material_key = sha, md5, key
            if self.diagnostic_suppressed_key == key:
                return
            song, material = find_material_for_chart(self.env.song_db, CHART_ANALYSIS_DB, self.env.root, sha, md5)
            if song is None or material is None:
                self.hide_maker_diagnostic(False)
                return
            self.maker_diagnostic.show_material(key, song, material)
        except Exception:
            LOGGER.exception("current song diagnostic polling failed")
        finally:
            pass

    def reanalyze_current_material(self) -> None:
        if not self.env or not self.current_material_key:
            return
        self.maker_diagnostic.status_var.set("素材状態：再解析中...")
        sha, md5 = self.current_material_sha, self.current_material_md5

        def done(result: Any, error: Exception | None) -> None:
            if error:
                self.maker_diagnostic.status_var.set("素材状態：再解析失敗")
                messagebox.showerror(APP_NAME, str(error), parent=self.maker_diagnostic)
                return
            song, material = result
            if song is not None and material is not None:
                self.maker_diagnostic.show_material(self.current_material_key, song, material)

        self.run_worker(
            lambda: reanalyze_material_for_chart(self.env.song_db, CHART_ANALYSIS_DB, self.env.root, sha, md5),
            done,
        )

    def open_current_material_folder(self) -> None:
        folder = self.maker_diagnostic.current_folder
        if folder:
            try:
                open_path(Path(folder))
            except Exception as exc:
                messagebox.showerror(APP_NAME, str(exc), parent=self.maker_diagnostic)

    def open_folder_dir(self) -> None:
        if self.env:
            open_path(self.env.default_json.parent)

    def open_table_dir(self) -> None:
        if self.env:
            open_path(self.env.table_dir)

    def open_config(self) -> None:
        if self.env:
            open_path(self.env.config)

    def enqueue_progress(self, update: ProgressUpdate) -> None:
        # Keep only the newest event so tens of thousands of chart/folder updates
        # cannot make the GUI lag behind the worker.
        try:
            self.progress_queue.put_nowait(update)
            return
        except queue.Full:
            pass
        try:
            self.progress_queue.get_nowait()
        except queue.Empty:
            pass
        try:
            self.progress_queue.put_nowait(update)
        except queue.Full:
            pass

    def start_progress_display(self, task_name: str) -> None:
        now = time.monotonic()
        self.progress_active = True
        self.progress_task_name = task_name
        self.progress_started_at = now
        self.progress_last_update_at = now
        self.progress_last_change_at = now
        self.progress_last_overall = 0.0
        self.progress_overall_total = 1.0
        self.progress_samples = [(now, 0.0)]
        self.progress_value_var.set(0.0)
        self.progress_percent_var.set("0.0%")
        self.progress_state_var.set("開始")
        self.progress_stage_var.set(task_name)
        self.progress_detail_var.set("処理を開始しています")
        self.progress_time_var.set("経過 00:00 / 残り 推定中")
        while True:
            try:
                self.progress_queue.get_nowait()
            except queue.Empty:
                break

    def _estimate_remaining_seconds(self, now: float) -> float | None:
        if not self.progress_active or self.progress_overall_total <= 0:
            return None
        current = self.progress_last_overall
        if current <= 0 or current >= self.progress_overall_total:
            return 0.0 if current >= self.progress_overall_total else None
        samples = [(t, value) for t, value in self.progress_samples if now - t <= 90.0]
        # Prefer a recent speed estimate after at least a few seconds of movement.
        rate = 0.0
        if len(samples) >= 2:
            first_t, first_value = samples[0]
            last_t, last_value = samples[-1]
            if last_t - first_t >= 3.0 and last_value > first_value:
                rate = (last_value - first_value) / (last_t - first_t)
        if rate <= 0 and now - self.progress_started_at >= 3.0:
            rate = current / max(now - self.progress_started_at, 0.001)
        if rate <= 0:
            return None
        return max(0.0, (self.progress_overall_total - current) / rate)

    def _apply_progress_update(self, update: ProgressUpdate) -> None:
        if not self.progress_active:
            return
        now = time.monotonic()
        previous = self.progress_last_overall
        self.progress_overall_total = max(float(update.overall_total), 0.001)
        self.progress_last_overall = max(previous, float(update.overall_current))
        self.progress_last_update_at = now
        if self.progress_last_overall > previous + 1e-9:
            self.progress_last_change_at = now
            self.progress_samples.append((now, self.progress_last_overall))
            self.progress_samples = self.progress_samples[-200:]
        fraction = min(1.0, max(0.0, self.progress_last_overall / self.progress_overall_total))
        stage_total = max(1, int(update.stage_total))
        stage_current = min(stage_total, max(0, int(update.stage_current)))
        self.progress_value_var.set(fraction * 100.0)
        self.progress_percent_var.set(f"{fraction * 100.0:.1f}%")
        self.progress_state_var.set("進行中")
        self.progress_stage_var.set(
            f"{update.stage_label}  {stage_current:,}/{stage_total:,}"
        )
        self.progress_detail_var.set(update.message or "処理中")
        eta = self._estimate_remaining_seconds(now)
        elapsed = now - self.progress_started_at
        eta_text = "推定中" if eta is None or fraction < 0.015 else format_duration(eta)
        self.progress_time_var.set(f"経過 {format_duration(elapsed)} / 残り {eta_text}")

    def finish_progress_display(self, success: bool, detail: str = "") -> None:
        now = time.monotonic()
        elapsed = now - self.progress_started_at if self.progress_started_at else 0.0
        self.progress_active = False
        if success:
            self.progress_last_overall = self.progress_overall_total
            self.progress_value_var.set(100.0)
            self.progress_percent_var.set("100.0%")
            self.progress_state_var.set("完了")
            self.progress_stage_var.set(self.progress_task_name or "処理完了")
            self.progress_detail_var.set("正常に完了しました")
            self.progress_time_var.set(f"所要時間 {format_duration(elapsed)}")
        else:
            self.progress_state_var.set("失敗")
            self.progress_stage_var.set(self.progress_task_name or "処理失敗")
            self.progress_detail_var.set(detail or "エラーで停止しました")
            self.progress_time_var.set(f"経過 {format_duration(elapsed)}")

    def poll_progress(self) -> None:
        try:
            while True:
                update = self.progress_queue.get_nowait()
                self._apply_progress_update(update)
        except queue.Empty:
            pass
        if self.progress_active:
            now = time.monotonic()
            elapsed = now - self.progress_started_at
            idle = now - self.progress_last_change_at
            worker_alive = bool(self.worker_thread and self.worker_thread.is_alive())
            if worker_alive:
                if idle < 10.0:
                    state = "進行中"
                elif idle < 45.0:
                    state = f"処理中・更新待ち {format_duration(idle)}"
                else:
                    state = f"停止疑い・更新なし {format_duration(idle)}"
                self.progress_state_var.set(state)
            eta = self._estimate_remaining_seconds(now)
            fraction = self.progress_last_overall / max(self.progress_overall_total, 0.001)
            eta_text = "推定中" if eta is None or fraction < 0.015 else format_duration(eta)
            self.progress_time_var.set(f"経過 {format_duration(elapsed)} / 残り {eta_text}")
        self.root.after(250, self.poll_progress)

    def run_worker(self, task: Callable[[], Any], callback: Callable[[Any, Exception | None], None]) -> bool:
        if self.busy:
            messagebox.showinfo(APP_NAME, "別の処理を実行中です", parent=self.root)
            return False
        self.busy = True

        def runner() -> None:
            result = None
            error: Exception | None = None
            try:
                result = task()
            except AnalysisCancelled as exc:
                error = exc
                LOGGER.info("analysis cancelled")
            except Exception as exc:
                error = exc
                LOGGER.exception("worker failed")
            self.worker_queue.put((callback, result, error))

        self.worker_thread = threading.Thread(target=runner, name="BMSCF-Worker", daemon=True)
        self.worker_thread.start()
        return True

    def poll_update_results(self) -> None:
        try:
            while True:
                callback = self.update_result_queue.get_nowait()
                callback()
        except queue.Empty:
            pass
        try:
            self.root.after(120, self.poll_update_results)
        except tk.TclError:
            pass

    def poll_worker(self) -> None:
        try:
            while True:
                callback, result, error = self.worker_queue.get_nowait()
                self.busy = False
                callback(result, error)
        except queue.Empty:
            pass
        self.root.after(100, self.poll_worker)

    def report_callback_exception(self, exc_type, exc_value, exc_tb) -> None:
        text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        LOGGER.error(text)
        messagebox.showerror(APP_NAME, f"GUIエラーが発生しました。\n\n{exc_value}\n\nログ: {LOG_PATH}", parent=self.root)

    def on_close(self) -> None:
        try:
            if self.simple_filter_editor is not None:
                self.simple_filter_editor.commit_all()
            self.quick_panel.commit_all()
            if self.mode == "detailed":
                self.settings.main_geometry = self.root.geometry()
            else:
                self.settings.simple_geometry = self.root.geometry()
            self.settings.quick_geometry = self.quick_panel.geometry()
            if self.maker_diagnostic is not None:
                self.settings.maker_diagnostic_geometry = self.maker_diagnostic.geometry()
            self.save_settings_now()
        finally:
            self.root.destroy()


def main() -> int:
    root = tk.Tk()
    try:
        style = ttk.Style(root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
    except Exception:
        pass
    MainApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
