"""
ORBAS Native PDF Extractor - GUI.

Native Tkinter interface. Chosen for reliability and speed:
  * Instant startup, tiny footprint (no embedded browser / WebView2, no HTTP server).
  * Extraction runs on a background thread; the UI thread is never blocked, so the
    window can never go "Not Responding".
  * Clipboard uses the native Tk clipboard - instant, no subprocess / PowerShell.
No artificial progress delays. The extraction engine (extractor.py) is unchanged.
"""

import os
import re
import sys
import json
import queue
import threading

import tkinter as tk
from tkinter import ttk, filedialog, scrolledtext

try:
    # Optional: enables OS-level drag-and-drop of a PDF onto the drop zone.
    from tkinterdnd2 import DND_FILES, TkinterDnD
    _DND_AVAILABLE = True
except Exception:
    _DND_AVAILABLE = False

from .config import APP_NAME, VERSION, JURISDICTIONS, REPORT_TYPES
from .extractor import extract_pdf, detect_jurisdiction
from .license import validate_license

DEMO_KEYS = {"ORBAS-DEMO-2026", "ORBAS-TRIAL-2026", "ORBAS-NSW-VALID"}

# Brand palette
BLUE = "#0453ed"
GREEN = "#096e4d"
ORANGE = "#fd6207"
DARK = "#0f172a"
BG = "#f1f5f9"
CARD = "#ffffff"
MUTED = "#64748b"
BORDER = "#e2e8f0"
JSON_BG = "#020617"
JSON_FG = "#86efac"
OK_BG = "#f0fdf4"
OK_FG = "#166534"
ERR_BG = "#fef2f2"
ERR_FG = "#991b1b"


def _enable_dpi_awareness():
    """Crisp text on Windows high-DPI displays."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


class OrbasApp:
    def __init__(self, root):
        self.root = root
        self.pdf_path = None
        self.pdf_size_mb = None
        self.license_verified = False
        self.extracted_json = ""
        self.extracting = False
        self._queue = queue.Queue()

        root.title(f"{APP_NAME} Native PDF Extractor")
        root.geometry("1180x800")
        root.minsize(980, 680)
        root.configure(bg=BG)

        self._init_style()
        self._build_ui()
        self._setup_dnd()
        self._poll_queue()

    # ---- styling -------------------------------------------------------
    def _init_style(self):
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        ui = "Segoe UI" if sys.platform == "win32" else "DejaVu Sans"
        self.font_ui = (ui, 11)
        self.font_bold = (ui, 11, "bold")
        self.font_h1 = (ui, 19, "bold")
        self.font_h2 = (ui, 12, "bold")
        self.font_small = (ui, 10)
        self.font_mono = ("Consolas" if sys.platform == "win32" else "DejaVu Sans Mono", 10)

        style.configure("Card.TFrame", background=CARD)
        style.configure("Bg.TFrame", background=BG)
        style.configure("TLabel", background=CARD, foreground=DARK, font=self.font_ui)
        style.configure("Bg.TLabel", background=BG, foreground=DARK, font=self.font_ui)
        style.configure("Muted.TLabel", background=CARD, foreground=MUTED, font=self.font_small)
        style.configure("H2.TLabel", background=CARD, foreground=DARK, font=self.font_h2)

        # Taller, roomier dropdowns (client asked for more height / better look).
        style.configure(
            "Orbas.TCombobox",
            font=self.font_ui,
            padding=(10, 8),
            arrowsize=16,
            relief="flat",
            fieldbackground="white",
            background="white",
            bordercolor="#cbd5e1",
            lightcolor="#cbd5e1",
            darkcolor="#cbd5e1",
        )
        style.map(
            "Orbas.TCombobox",
            fieldbackground=[("readonly", "white")],
            bordercolor=[("focus", BLUE)],
        )
        self.root.option_add("*TCombobox*Listbox.font", self.font_ui)
        self.root.option_add("*TCombobox*Listbox.background", "white")
        self.root.option_add("*TCombobox*Listbox.selectBackground", BLUE)
        style.configure("Orbas.Horizontal.TProgressbar", background=GREEN,
                        troughcolor=BORDER, thickness=8)

    def _accent_button(self, parent, text, color, command, big=False):
        """A flat coloured button (tk.Button gives us full colour control)."""
        btn = tk.Button(
            parent, text=text, command=command,
            bg=color, fg="white", activebackground=color, activeforeground="white",
            relief="flat", bd=0, cursor="hand2",
            font=(self.font_bold if big else self.font_ui),
            padx=(18 if big else 12), pady=(9 if big else 6),
        )
        return btn

    def _card(self, parent):
        outer = tk.Frame(parent, bg=BORDER)  # 1px border effect
        inner = tk.Frame(outer, bg=CARD)
        inner.pack(fill="both", expand=True, padx=1, pady=1)
        return outer, inner

    def _step_header(self, parent, num, title, color):
        bar = tk.Frame(parent, bg=CARD)
        bar.pack(fill="x", padx=14, pady=(12, 6))
        circ = tk.Label(bar, text=str(num), bg=color, fg="white", font=self.font_bold,
                        width=2, height=1)
        circ.pack(side="left")
        tk.Label(bar, text=title, bg=CARD, fg=DARK, font=self.font_h2).pack(side="left", padx=8)

    # ---- layout --------------------------------------------------------
    def _build_ui(self):
        # Header
        header = tk.Frame(self.root, bg=BG)
        header.pack(fill="x", padx=18, pady=(14, 6))
        left = tk.Frame(header, bg=BG)
        left.pack(side="left")
        tk.Label(left, text=f"{APP_NAME} Native PDF Extractor", bg=BG, fg=BLUE,
                 font=self.font_h1).pack(anchor="w")
        tk.Label(left, text="Extract rental condition report PDF data into structured JSON.",
                 bg=BG, fg=MUTED, font=self.font_small).pack(anchor="w")
        rt = tk.Frame(header, bg=BG)
        rt.pack(side="right")
        tk.Label(rt, text=f"v{VERSION}", bg=BG, fg=DARK, font=self.font_bold).pack(anchor="e")
        tk.Label(rt, text="Local PDF Extraction", bg=BG, fg=MUTED,
                 font=self.font_small).pack(anchor="e")

        # Body: two columns
        body = tk.Frame(self.root, bg=BG)
        body.pack(fill="both", expand=True, padx=18, pady=(4, 16))
        body.columnconfigure(0, weight=1, uniform="col")
        body.columnconfigure(1, weight=1, uniform="col")
        body.rowconfigure(0, weight=1)

        left_col = tk.Frame(body, bg=BG)
        left_col.grid(row=0, column=0, sticky="nsew", padx=(0, 9))
        right_col = tk.Frame(body, bg=BG)
        right_col.grid(row=0, column=1, sticky="nsew", padx=(9, 0))

        self._build_left(left_col)
        self._build_right(right_col)

    def _build_left(self, parent):
        # Step 1 - Select PDF
        c1o, c1 = self._card(parent)
        c1o.pack(fill="x", pady=(0, 10))
        self._step_header(c1, 1, "Select PDF File", BLUE)
        self.dz_bg = "#f8fafc"
        dz = tk.Frame(c1, bg=self.dz_bg, highlightbackground="#cbd5e1",
                      highlightcolor="#cbd5e1", highlightthickness=2, bd=0)
        dz.pack(fill="x", padx=14, pady=(0, 12))
        self.dropzone = dz
        inner = tk.Frame(dz, bg=self.dz_bg)
        inner.pack(pady=10)
        self.dz_icon = tk.Label(inner, text="\U0001F4C4", bg=self.dz_bg,
                                fg="#94a3b8", font=(self.font_ui[0], 20))
        self.dz_icon.pack(side="left", padx=(0, 12))
        txt = tk.Frame(inner, bg=self.dz_bg)
        txt.pack(side="left")
        self.dz_main = tk.Label(txt, text="Drag & drop your PDF here", bg=self.dz_bg,
                                fg=DARK, font=self.font_bold, anchor="w")
        self.dz_main.pack(anchor="w")
        self.dz_hint = tk.Label(txt, text="or use the Browse button",
                                bg=self.dz_bg, fg=MUTED, font=self.font_small, anchor="w")
        self.dz_hint.pack(anchor="w")
        self.browse_btn = self._accent_button(txt, "Browse PDF", DARK, self.on_browse)
        self.browse_btn.pack(anchor="w", pady=(8, 0))
        self.file_label = tk.Label(c1, text="No file selected.", bg=CARD, fg=MUTED,
                                   font=self.font_small, anchor="w", justify="left")
        self.file_label.pack(fill="x", padx=14, pady=(0, 14))

        # Step 2 - Jurisdiction & Doc type
        c2o, c2 = self._card(parent)
        c2o.pack(fill="x", pady=(0, 10))
        self._step_header(c2, 2, "Jurisdiction & Document Type", BLUE)
        row = tk.Frame(c2, bg=CARD)
        row.pack(fill="x", padx=14, pady=(0, 12))
        row.columnconfigure(0, weight=1)
        row.columnconfigure(1, weight=1)
        tk.Label(row, text="Jurisdiction", bg=CARD, fg=DARK, font=self.font_small).grid(
            row=0, column=0, sticky="w")
        tk.Label(row, text="Document Type", bg=CARD, fg=DARK, font=self.font_small).grid(
            row=0, column=1, sticky="w", padx=(8, 0))
        self.jur_var = tk.StringVar()
        jur_values = ["Auto Detect"] + [f"{code} - {name}" for code, name in JURISDICTIONS]
        self.jur_box = ttk.Combobox(row, textvariable=self.jur_var, values=jur_values,
                                    state="readonly", style="Orbas.TCombobox",
                                    font=self.font_ui, height=12)
        self.jur_box.current(0)
        self.jur_box.grid(row=1, column=0, sticky="ew", pady=(4, 0), ipady=3)
        self.doc_var = tk.StringVar()
        doc_values = [name for _, name in REPORT_TYPES]
        self.doc_box = ttk.Combobox(row, textvariable=self.doc_var, values=doc_values,
                                    state="readonly", style="Orbas.TCombobox",
                                    font=self.font_ui, height=12)
        self.doc_box.current(0)
        self.doc_box.grid(row=1, column=1, sticky="ew", padx=(8, 0), pady=(4, 0), ipady=3)

        # Step 3 - License
        c3o, c3 = self._card(parent)
        c3o.pack(fill="x", pady=(0, 10))
        self._step_header(c3, 3, "Product Key Verification", GREEN)
        lrow = tk.Frame(c3, bg=CARD)
        lrow.pack(fill="x", padx=14, pady=(0, 4))
        self.key_var = tk.StringVar()
        key_wrap = tk.Frame(lrow, bg="#cbd5e1")
        key_wrap.pack(side="left", fill="x", expand=True)
        self.key_entry = tk.Entry(key_wrap, textvariable=self.key_var, font=self.font_mono,
                                  relief="flat", bd=0, highlightthickness=0)
        self.key_entry.pack(fill="x", expand=True, padx=1, pady=1, ipady=8, ipadx=6)
        self.key_entry.bind("<Return>", lambda e: self.on_verify())
        self.key_entry.bind("<KeyRelease>", self._on_key_typed)
        self.verify_btn = self._accent_button(lrow, "Verify", GREEN, self.on_verify)
        self.verify_btn.pack(side="left", padx=(8, 0))
        self.lic_label = tk.Label(c3, text="Enter your product key (e.g. ORBAS-DEMO-2026).",
                                  bg=CARD, fg=MUTED, font=self.font_small, anchor="w",
                                  justify="left", wraplength=460)
        self.lic_label.pack(fill="x", padx=14, pady=(0, 12))

        # Step 4 - Extract
        c4o, c4 = self._card(parent)
        c4o.pack(fill="x")
        self._step_header(c4, 4, "Extract PDF", ORANGE)
        self.extract_btn = self._accent_button(c4, "Extract PDF", ORANGE, self.on_extract, big=True)
        self.extract_btn.configure(state="disabled", bg="#cbd5e1")
        self.extract_btn.pack(fill="x", padx=14, pady=(0, 6))
        self.progress = ttk.Progressbar(c4, mode="indeterminate",
                                        style="Orbas.Horizontal.TProgressbar")
        self.status_label = tk.Label(c4, text="", bg=CARD, fg=MUTED, font=self.font_small,
                                     anchor="w", justify="left", wraplength=460)
        self.status_label.pack(fill="x", padx=14, pady=(0, 12))

    def _build_right(self, parent):
        co, c = self._card(parent)
        co.pack(fill="both", expand=True)

        top = tk.Frame(c, bg=CARD)
        top.pack(fill="x", padx=14, pady=(12, 6))
        htl = tk.Frame(top, bg=CARD)
        htl.pack(side="left")
        circ = tk.Label(htl, text="5", bg=GREEN, fg="white", font=self.font_bold, width=2)
        circ.pack(side="left")
        tt = tk.Frame(htl, bg=CARD)
        tt.pack(side="left", padx=8)
        tk.Label(tt, text="JSON Output", bg=CARD, fg=DARK, font=self.font_h2).pack(anchor="w")
        tk.Label(tt, text="Select text and press Ctrl+C, or use the Copy JSON button.",
                 bg=CARD, fg=MUTED, font=self.font_small).pack(anchor="w")
        self.copy_btn = self._accent_button(top, "Copy JSON", DARK, self.on_copy)
        self.copy_btn.configure(state="disabled", bg="#cbd5e1")
        self.copy_btn.pack(side="right")

        # Metadata panel (stat tiles) - populated after extraction.
        self.meta_card = tk.Frame(c, bg=BORDER)
        self.json_card_body = c

        self.json_text = scrolledtext.ScrolledText(
            c, bg=JSON_BG, fg=JSON_FG, insertbackground=JSON_FG,
            font=self.font_mono, wrap="none", relief="flat", bd=0,
            padx=10, pady=8,
        )
        self.json_text.pack(fill="both", expand=True, padx=14, pady=(6, 14))
        self.json_text.insert("1.0", "No extraction output yet.")
        self.json_text.configure(state="disabled")
        # Native copy / select-all shortcuts
        self.json_text.bind("<Control-a>", self._select_all_json)
        self.json_text.bind("<Control-A>", self._select_all_json)

        self.copy_ok = tk.Label(c, text="", bg=CARD, fg=OK_FG, font=self.font_small,
                                anchor="w")

    # ---- helpers -------------------------------------------------------
    def _set_status(self, widget, text, kind="muted"):
        colors = {"muted": MUTED, "ok": OK_FG, "err": ERR_FG}
        widget.configure(text=text, fg=colors.get(kind, MUTED))

    def _check_ready(self):
        ready = bool(self.pdf_path) and self.license_verified and not self.extracting
        if ready:
            self.extract_btn.configure(state="normal", bg=ORANGE)
        else:
            self.extract_btn.configure(state="disabled", bg="#cbd5e1")

    def _selected_jurisdiction(self):
        val = self.jur_var.get()
        if val.startswith("Auto"):
            return "auto"
        return val.split(" - ")[0]

    def _selected_doctype(self):
        name = self.doc_var.get()
        for code, label in REPORT_TYPES:
            if label == name:
                return code
        return "auto"

    # ---- drag and drop -------------------------------------------------
    def _setup_dnd(self):
        if not _DND_AVAILABLE:
            # No DnD library bundled - the drop zone still works as a Browse click.
            self.dz_main.configure(text="Choose a PDF from your computer")
            self.dz_hint.configure(text="click Browse to select a file")
            return
        try:
            self.dropzone.drop_target_register(DND_FILES)
            self.dropzone.dnd_bind("<<Drop>>", self._on_drop)
            self.dropzone.dnd_bind("<<DropEnter>>", self._on_drop_enter)
            self.dropzone.dnd_bind("<<DropLeave>>", self._on_drop_leave)
        except Exception:
            pass

    def _recolor_dropzone(self, bg):
        def walk(w):
            for child in w.winfo_children():
                if isinstance(child, tk.Frame) or isinstance(child, tk.Label):
                    try:
                        child.configure(bg=bg)
                    except tk.TclError:
                        pass
                walk(child)
        self.dropzone.configure(bg=bg)
        walk(self.dropzone)

    def _on_drop_enter(self, event):
        self.dropzone.configure(highlightbackground=BLUE, highlightcolor=BLUE)
        self._recolor_dropzone("#eff6ff")
        return event.action

    def _on_drop_leave(self, event):
        self.dropzone.configure(highlightbackground="#cbd5e1", highlightcolor="#cbd5e1")
        self._recolor_dropzone(self.dz_bg)
        return event.action

    def _on_drop(self, event):
        self._on_drop_leave(event)
        path = self._parse_dnd_path(event.data)
        if not path:
            return
        if not path.lower().endswith(".pdf") or not os.path.isfile(path):
            self.file_label.configure(text="Please drop a single PDF file.", fg=ERR_FG)
            return
        self._set_pdf(path)

    @staticmethod
    def _parse_dnd_path(data):
        # tkdnd may return "{C:\path with spaces\a.pdf}" or several space-joined paths.
        if not data:
            return None
        data = data.strip()
        braced = re.findall(r"\{([^}]*)\}", data)
        if braced:
            return braced[0]
        return data.split()[0]

    # ---- actions -------------------------------------------------------
    def _set_pdf(self, path):
        self.pdf_path = path
        self.pdf_size_mb = os.path.getsize(path) / (1024 * 1024)
        self.file_label.configure(
            text=f"Selected: {os.path.basename(path)}  ({self.pdf_size_mb:.2f} MB)", fg=OK_FG)
        self._check_ready()

    def on_browse(self):
        path = filedialog.askopenfilename(
            title="Select condition report PDF",
            filetypes=[("PDF files", "*.pdf"), ("All files", "*.*")],
        )
        if not path or not os.path.isfile(path):
            return
        self._set_pdf(path)

    def _on_key_typed(self, event=None):
        if event and event.keysym in ("Return", "KP_Enter"):
            return
        self.license_verified = False
        self._check_ready()

    def on_verify(self):
        key = self.key_var.get().strip()
        if not key:
            self._set_status(self.lic_label, "Please enter a product key.", "err")
            return

        # Demo/offline keys verify instantly - no network round trip.
        if key.upper() in DEMO_KEYS:
            self.license_verified = True
            self._set_status(self.lic_label,
                             "Product key verified. PDF extraction is now enabled.", "ok")
            self._check_ready()
            return

        self.verify_btn.configure(state="disabled", text="Verifying...")
        self._set_status(self.lic_label, "Checking product key...", "muted")

        def worker():
            try:
                result = validate_license(key)
                ok = bool(result.get("valid"))
                msg = ("Product key verified. PDF extraction is now enabled." if ok
                       else result.get("error") or "Invalid product key.")
            except Exception as e:
                ok, msg = False, f"Verification error: {e}"
            self._queue.put(("license", ok, msg))

        threading.Thread(target=worker, daemon=True).start()

    def on_extract(self):
        if self.extracting or not self.pdf_path:
            return
        self.extracting = True
        self._check_ready()
        self.copy_btn.configure(state="disabled", bg="#cbd5e1")
        self.copy_ok.pack_forget()
        self.meta_card.pack_forget()
        self._set_status(self.status_label, "Extracting condition report data...", "muted")
        self.progress.pack(fill="x", padx=14, pady=(0, 4))
        self.progress.start(12)
        self._set_json("Extracting PDF data, please wait...")

        jur = self._selected_jurisdiction()
        doc = self._selected_doctype()
        path = self.pdf_path

        def worker():
            try:
                detected = detect_jurisdiction(path) if jur == "auto" else jur
                result = extract_pdf(
                    path, jurisdiction=detected,
                    report_type=doc, output_dir=None, save_images=False,
                )
                self._queue.put(("extract_ok", result))
            except Exception as e:
                self._queue.put(("extract_err", str(e)))

        threading.Thread(target=worker, daemon=True).start()

    def on_copy(self):
        if not self.extracted_json:
            return
        # Native Tk clipboard - instant, no subprocess.
        self.root.clipboard_clear()
        self.root.clipboard_append(self.extracted_json)
        self.root.update_idletasks()
        self._show_copied("JSON copied to clipboard. You can now paste with Ctrl+V.")

    def _select_all_json(self, event=None):
        self.json_text.tag_add("sel", "1.0", "end-1c")
        self.json_text.mark_set("insert", "1.0")
        self.json_text.see("insert")
        return "break"

    def _show_copied(self, text):
        self.copy_ok.configure(text=text)
        self.copy_ok.pack(fill="x", padx=14, pady=(0, 10))
        self.root.after(4000, self.copy_ok.pack_forget)

    def _set_json(self, text):
        self.json_text.configure(state="normal")
        self.json_text.delete("1.0", "end")
        self.json_text.insert("1.0", text)
        self.json_text.configure(state="disabled")

    # ---- queue pump (thread -> UI) -------------------------------------
    def _poll_queue(self):
        try:
            while True:
                msg = self._queue.get_nowait()
                self._handle_message(msg)
        except queue.Empty:
            pass
        self.root.after(60, self._poll_queue)

    def _handle_message(self, msg):
        kind = msg[0]
        if kind == "license":
            _, ok, text = msg
            self.license_verified = ok
            self.verify_btn.configure(state="normal", text="Verify")
            self._set_status(self.lic_label, text, "ok" if ok else "err")
            self._check_ready()
        elif kind == "extract_ok":
            result = msg[1]
            self.progress.stop()
            self.progress.pack_forget()
            self.extracting = False
            self.extracted_json = json.dumps(result, indent=2, ensure_ascii=False)
            self._set_json(self.extracted_json)
            self.copy_btn.configure(state="normal", bg=DARK)
            self._set_status(self.status_label, "Extraction completed successfully.", "ok")
            self._show_summary(result)
            self._check_ready()
        elif kind == "extract_err":
            self.progress.stop()
            self.progress.pack_forget()
            self.extracting = False
            self._set_json("Extraction failed.")
            self._set_status(self.status_label, f"Error: {msg[1]}", "err")
            self._check_ready()

    @staticmethod
    def _human_size(num_bytes):
        size = float(num_bytes)
        for unit in ("B", "KB", "MB"):
            if size < 1024 or unit == "MB":
                return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
            size /= 1024
        return f"{size:.1f} MB"

    def _stat_tile(self, parent, col, label, value, value_fg=DARK):
        cell = tk.Frame(parent, bg="white")
        cell.grid(row=0, column=col, sticky="nsew", padx=2)
        tk.Label(cell, text=str(value), bg="white", fg=value_fg,
                 font=(self.font_ui[0], 14, "bold")).pack()
        tk.Label(cell, text=label.upper(), bg="white", fg="#94a3b8",
                 font=(self.font_ui[0], 8, "bold")).pack(pady=(1, 0))

    def _show_summary(self, result):
        for ch in self.meta_card.winfo_children():
            ch.destroy()

        areas = result.get("areas", [])
        comps = sum(len(a.get("components", [])) for a in areas)
        meta = result.get("report_metadata", {})
        missing = []
        if not meta.get("address"):
            missing.append("Property Address")
        if not meta.get("tenant_name"):
            missing.append("Tenant Name")
        if not meta.get("landlord_name"):
            missing.append("Landlord Name")
        passed = not missing

        fname = meta.get("source_file") or (
            os.path.basename(self.pdf_path) if self.pdf_path else "N/A")
        pdf_size = f"{self.pdf_size_mb:.2f} MB" if self.pdf_size_mb is not None else "N/A"
        json_size = self._human_size(len(self.extracted_json.encode("utf-8")))

        inner = tk.Frame(self.meta_card, bg="white")
        inner.pack(fill="x", padx=1, pady=1)

        # Header: source file (left) + validation badge (right)
        hdr = tk.Frame(inner, bg="white")
        hdr.pack(fill="x", padx=14, pady=(11, 8))
        fl = tk.Frame(hdr, bg="white")
        fl.pack(side="left", fill="x", expand=True)
        tk.Label(fl, text="SOURCE FILE", bg="white", fg="#94a3b8",
                 font=(self.font_ui[0], 8, "bold")).pack(anchor="w")
        tk.Label(fl, text=fname, bg="white", fg=DARK, font=self.font_bold,
                 anchor="w").pack(anchor="w")
        badge_bg = "#dcfce7" if passed else "#fef3c7"
        badge_fg = "#15803d" if passed else "#b45309"
        tk.Label(hdr, text=("VALIDATION  PASSED" if passed else "VALIDATION  REVIEW"),
                 bg=badge_bg, fg=badge_fg, font=(self.font_ui[0], 9, "bold"),
                 padx=12, pady=6).pack(side="right", anchor="n")

        tk.Frame(inner, bg="#eef2f7", height=1).pack(fill="x", padx=14)

        # Stat tiles
        tiles = tk.Frame(inner, bg="white")
        tiles.pack(fill="x", padx=12, pady=(10, 12))
        stats = [
            ("Jurisdiction", result.get("jurisdiction", "N/A")),
            ("Doc Type", result.get("document_type", "N/A")),
            ("Pages", meta.get("total_pages", 0)),
            ("Areas", len(areas)),
            ("Records", comps),
            ("PDF Size", pdf_size),
            ("JSON Size", json_size),
        ]
        col = 0
        for i, (label, value) in enumerate(stats):
            tiles.columnconfigure(col, weight=1, uniform="tile")
            self._stat_tile(tiles, col, label, value)
            col += 1
            if i < len(stats) - 1:
                div = tk.Frame(tiles, bg="#eef2f7", width=1)
                div.grid(row=0, column=col, sticky="ns", pady=2)
                col += 1

        if missing:
            tk.Label(inner, text="Not found in this PDF: " + ", ".join(missing),
                     bg="white", fg="#b45309", font=self.font_small, anchor="w").pack(
                     fill="x", padx=14, pady=(0, 10))

        self.meta_card.pack(fill="x", padx=14, pady=(2, 8), before=self.json_text)


def run_gui():
    _enable_dpi_awareness()
    # TkinterDnD.Tk() is a drop-in Tk root that also enables file drag-and-drop.
    if _DND_AVAILABLE:
        try:
            root = TkinterDnD.Tk()
        except Exception:
            root = tk.Tk()
    else:
        root = tk.Tk()
    OrbasApp(root)
    root.mainloop()
