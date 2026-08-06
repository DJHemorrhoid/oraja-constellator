from __future__ import annotations

import datetime as _dt
import sys
import traceback
from pathlib import Path

PYTHON_DIR = Path(__file__).resolve().parent
APP_DIR = PYTHON_DIR.parent
LOG_DIR = APP_DIR / "logs"
LOG_PATH = LOG_DIR / "startup_error.log"

if str(PYTHON_DIR) not in sys.path:
    sys.path.insert(0, str(PYTHON_DIR))


def _record_failure(text: str) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write("\n" + "=" * 72 + "\n")
        handle.write(_dt.datetime.now().isoformat(timespec="seconds") + "\n")
        handle.write(text.rstrip() + "\n")


def _show_failure(text: str) -> None:
    print(text, file=sys.stderr)
    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(
            "oraja-constellator",
            "起動中にエラーが発生しました。\n\n"
            + text.splitlines()[-1]
            + f"\n\nログ: {LOG_PATH}",
            parent=root,
        )
        root.destroy()
    except Exception:
        pass


def main() -> int:
    try:
        import oraja_constellator

        return int(oraja_constellator.main())
    except KeyboardInterrupt:
        return 130
    except BaseException:
        text = traceback.format_exc()
        _record_failure(text)
        _show_failure(text)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
