import sys
import os
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def get_log_path():
    if getattr(sys, 'frozen', False):
        return os.path.join(os.path.dirname(sys.executable), "ORBAS_error.log")
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "ORBAS_error.log")


if __name__ == "__main__":
    try:
        if len(sys.argv) <= 1 or (len(sys.argv) == 1 and getattr(sys, 'frozen', False)):
            from src.gui import run_gui
            run_gui()
        else:
            from src.cli import main
            main()
    except Exception as e:
        log_path = get_log_path()
        with open(log_path, "w") as f:
            f.write(f"ORBAS crashed at startup:\n\n{traceback.format_exc()}")
        try:
            import tkinter as tk
            from tkinter import messagebox
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror("ORBAS Error",
                f"Application failed to start.\n\n{str(e)}\n\nDetails saved to:\n{log_path}")
            root.destroy()
        except Exception:
            pass
        sys.exit(1)
