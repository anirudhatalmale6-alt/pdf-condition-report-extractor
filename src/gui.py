import os
import json
import time
import socket
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from .config import APP_NAME, VERSION, JURISDICTIONS, REPORT_TYPES
from .extractor import extract_pdf, detect_jurisdiction, detect_report_type_standalone
from .license import validate_license

DEMO_KEYS = {"ORBAS-DEMO-2026", "ORBAS-TRIAL-2026", "ORBAS-NSW-VALID"}


def check_internet():
    try:
        socket.create_connection(("8.8.8.8", 53), timeout=5)
        return True
    except OSError:
        return False


class ExtractorApp:
    BG = "#f1f5f9"
    CARD_BG = "#ffffff"
    BLUE = "#0453ed"
    GREEN = "#096e4d"
    ORANGE = "#fd6207"
    DARK = "#0f172a"
    SLATE_300 = "#cbd5e1"
    SLATE_500 = "#64748b"
    SLATE_600 = "#475569"
    JSON_BG = "#020617"
    JSON_FG = "#86efac"
    OK_BG = "#f0fdf4"
    OK_FG = "#166534"
    ERR_BG = "#fef2f2"
    ERR_FG = "#991b1b"
    INFO_BG = "#f0f9ff"
    INFO_FG = "#1e40af"

    def __init__(self, root):
        self.root = root
        self.root.title(f"{APP_NAME} Native PDF Extractor")
        self.root.geometry("1200x800")
        self.root.minsize(1000, 700)
        self.root.configure(bg=self.BG)

        self.pdf_path = None
        self.processing = False
        self.license_verified = False
        self.extracted_json = ""

        if not check_internet():
            self.root.withdraw()
            messagebox.showerror(
                "No Internet Connection",
                "NO INTERNET CONNECTION DETECTED.\n\n"
                "An active internet connection is required to verify "
                "your product license and use this application.\n\n"
                "Please connect to the internet and try again.")
            self.root.destroy()
            return

        self._setup_styles()
        self._build_ui()

    def _setup_styles(self):
        s = ttk.Style()
        s.theme_use("clam")
        s.configure("green.Horizontal.TProgressbar",
                     troughcolor="#e2e8f0", background=self.GREEN)

    def _circle(self, parent, num, color):
        c = tk.Canvas(parent, width=32, height=32,
                      bg=self.CARD_BG, highlightthickness=0)
        c.create_oval(2, 2, 30, 30, fill=color, outline=color)
        c.create_text(16, 16, text=str(num), fill="white",
                      font=("Verdana", 11, "bold"))
        return c

    def _card(self, parent, **kw):
        f = tk.Frame(parent, bg=self.CARD_BG, relief=tk.SOLID, bd=1,
                     padx=12, pady=8)
        f.pack(fill=kw.get("fill", tk.X),
               expand=kw.get("expand", False),
               pady=kw.get("pady", (0, 5)),
               padx=kw.get("padx", 0))
        return f

    def _card_hdr(self, card, step, text, color):
        row = tk.Frame(card, bg=self.CARD_BG)
        row.pack(fill=tk.X, pady=(0, 6))
        self._circle(row, step, color).pack(side=tk.LEFT, padx=(0, 8))
        tk.Label(row, text=text, font=("Verdana", 11, "bold"),
                 bg=self.CARD_BG).pack(side=tk.LEFT)
        return row

    def _btn(self, parent, text, cmd, bg, **kw):
        b = tk.Button(parent, text=text, command=cmd, bg=bg, fg="white",
                      font=kw.get("font", ("Verdana", 9, "bold")),
                      relief=tk.FLAT,
                      padx=kw.get("padx", 16), pady=kw.get("pady", 5),
                      cursor="hand2", activebackground=kw.get("abg", bg),
                      activeforeground="white",
                      disabledforeground="white")
        return b

    # ── Layout ───────────────────────────────────────────────────

    def _build_ui(self):
        main = tk.Frame(self.root, bg=self.BG, padx=16, pady=10)
        main.pack(fill=tk.BOTH, expand=True)

        hdr = tk.Frame(main, bg=self.BG)
        hdr.pack(fill=tk.X, pady=(0, 8))
        lh = tk.Frame(hdr, bg=self.BG)
        lh.pack(side=tk.LEFT)
        tk.Label(lh, text=f"{APP_NAME} Native PDF Extractor",
                 font=("Verdana", 17, "bold"), fg=self.BLUE,
                 bg=self.BG).pack(anchor=tk.W)
        tk.Label(lh, text="Extract rental condition report PDF data "
                          "into structured JSON.",
                 font=("Verdana", 9), fg=self.SLATE_600,
                 bg=self.BG).pack(anchor=tk.W)
        rh = tk.Frame(hdr, bg=self.BG)
        rh.pack(side=tk.RIGHT)
        tk.Label(rh, text=f"v{VERSION}", font=("Verdana", 10, "bold"),
                 bg=self.BG).pack(anchor=tk.E)
        tk.Label(rh, text="Local PDF Extraction",
                 font=("Verdana", 8), fg=self.SLATE_500,
                 bg=self.BG).pack(anchor=tk.E)

        body = tk.Frame(main, bg=self.BG)
        body.pack(fill=tk.BOTH, expand=True)
        left = tk.Frame(body, bg=self.BG)
        left.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 6))
        right = tk.Frame(body, bg=self.BG)
        right.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True, padx=(6, 0))

        self._step1(left)
        self._step2(left)
        self._step3(left)
        self._step4(left)
        self._step5(right)

    # ── Step 1: Select PDF ───────────────────────────────────────

    def _step1(self, p):
        card = self._card(p)
        self._card_hdr(card, 1, "Select PDF File", self.BLUE)

        zone = tk.Frame(card, bg="#f8fafc", bd=1, relief=tk.SOLID,
                        padx=14, pady=10)
        zone.pack(fill=tk.X)
        tk.Label(zone, text="\U0001F4C4", font=("Segoe UI", 16),
                 bg="#f8fafc").pack()
        tk.Label(zone, text="Click Browse to select your PDF file",
                 font=("Verdana", 9, "bold"), bg="#f8fafc").pack(pady=(2, 0))
        tk.Label(zone, text="Supported format: .pdf",
                 font=("Verdana", 8), fg=self.SLATE_500,
                 bg="#f8fafc").pack()
        self._btn(zone, "Browse PDF", self._browse_pdf, self.DARK,
                  abg="#1e293b").pack(pady=(5, 0))

        self._fi = tk.Frame(card, bg=self.OK_BG, padx=10, pady=8)
        self._fi_lbl = tk.Label(self._fi, font=("Verdana", 9),
                                bg=self.OK_BG, fg=self.OK_FG)
        self._fi_lbl.pack(anchor=tk.W)

    # ── Step 2: Jurisdiction & Document Type ──────────────────────

    def _step2(self, p):
        card = self._card(p)
        self._card_hdr(card, 2, "Jurisdiction & Document Type", self.BLUE)

        row = tk.Frame(card, bg=self.CARD_BG)
        row.pack(fill=tk.X)

        jf = tk.Frame(row, bg=self.CARD_BG)
        jf.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8))
        tk.Label(jf, text="Jurisdiction", font=("Verdana", 9, "bold"),
                 bg=self.CARD_BG).pack(anchor=tk.W, pady=(0, 3))
        self.jur_var = tk.StringVar(value="Auto Detect")
        cb1 = ttk.Combobox(jf, textvariable=self.jur_var, state="readonly",
                           font=("Verdana", 9))
        cb1["values"] = ["Auto Detect"] + \
                        [f"{c} - {n}" for c, n in JURISDICTIONS]
        cb1.pack(fill=tk.X)

        df = tk.Frame(row, bg=self.CARD_BG)
        df.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(8, 0))
        tk.Label(df, text="Document Type", font=("Verdana", 9, "bold"),
                 bg=self.CARD_BG).pack(anchor=tk.W, pady=(0, 3))
        self.doc_var = tk.StringVar(value="Auto Detect")
        cb2 = ttk.Combobox(df, textvariable=self.doc_var, state="readonly",
                           font=("Verdana", 9))
        cb2["values"] = [n for _, n in REPORT_TYPES]
        cb2.pack(fill=tk.X)

    # ── Step 3: License Verification ─────────────────────────────

    def _step3(self, p):
        card = self._card(p)
        self._card_hdr(card, 3, "Product Key Verification", self.GREEN)

        tk.Label(card, text="Product / License Key",
                 font=("Verdana", 9, "bold"),
                 bg=self.CARD_BG).pack(anchor=tk.W, pady=(0, 3))

        row = tk.Frame(card, bg=self.CARD_BG)
        row.pack(fill=tk.X)
        self.key_entry = ttk.Entry(row, font=("Consolas", 10))
        self.key_entry.pack(side=tk.LEFT, fill=tk.X, expand=True,
                            padx=(0, 8))
        self.key_entry.bind("<Return>", lambda e: self._verify())
        self.key_entry.bind("<KeyRelease>", lambda e: self._key_changed())

        self.vbtn = self._btn(row, "Verify", self._verify,
                              self.GREEN, abg="#065f46")
        self.vbtn.pack(side=tk.RIGHT)

        self._lm = tk.Frame(card, bg=self.CARD_BG)
        self._ll = tk.Label(self._lm, font=("Verdana", 9), wraplength=420)
        self._ll.pack(anchor=tk.W, padx=8, pady=6)

    # ── Step 4: Extract ──────────────────────────────────────────

    def _step4(self, p):
        card = self._card(p)
        self._card_hdr(card, 4, "Extract PDF", self.ORANGE)

        self.ebtn = tk.Button(
            card, text="Extract PDF", command=self._extract,
            bg=self.SLATE_300, fg="white",
            font=("Verdana", 10, "bold"), relief=tk.FLAT, pady=7,
            state=tk.DISABLED, cursor="arrow",
            disabledforeground="white")
        self.ebtn.pack(fill=tk.X)

        self._pf = tk.Frame(card, bg=self.CARD_BG)
        ph = tk.Frame(self._pf, bg=self.CARD_BG)
        ph.pack(fill=tk.X, pady=(0, 4))
        self._pt = tk.Label(ph, text="", font=("Verdana", 9, "bold"),
                            bg=self.CARD_BG)
        self._pt.pack(side=tk.LEFT)
        self._pp = tk.Label(ph, text="0%", font=("Verdana", 9),
                            bg=self.CARD_BG)
        self._pp.pack(side=tk.RIGHT)
        self._pb = ttk.Progressbar(
            self._pf, style="green.Horizontal.TProgressbar",
            mode="determinate", maximum=100)
        self._pb.pack(fill=tk.X)

        self._sf = tk.Frame(card, bg=self.CARD_BG)
        self._sl = tk.Label(self._sf, font=("Verdana", 9), wraplength=420)
        self._sl.pack(anchor=tk.W, padx=8, pady=6)

    # ── Step 5: JSON Output ──────────────────────────────────────

    def _step5(self, p):
        card = self._card(p, fill=tk.BOTH, expand=True)

        hdr = tk.Frame(card, bg=self.CARD_BG)
        hdr.pack(fill=tk.X, pady=(0, 8))
        lh = tk.Frame(hdr, bg=self.CARD_BG)
        lh.pack(side=tk.LEFT)
        self._circle(lh, 5, self.GREEN).pack(side=tk.LEFT, padx=(0, 10))
        tf = tk.Frame(lh, bg=self.CARD_BG)
        tf.pack(side=tk.LEFT)
        tk.Label(tf, text="JSON Output", font=("Verdana", 12, "bold"),
                 bg=self.CARD_BG).pack(anchor=tk.W)
        tk.Label(tf, text="Copy this output and paste it into the "
                          "designated ORBAS UI.",
                 font=("Verdana", 8), fg=self.SLATE_500,
                 bg=self.CARD_BG).pack(anchor=tk.W)
        self.cbtn = tk.Button(
            hdr, text="Copy JSON", command=self._copy_json,
            bg=self.SLATE_300, fg="white",
            font=("Verdana", 9, "bold"), relief=tk.FLAT,
            padx=14, pady=4, state=tk.DISABLED,
            disabledforeground="white")
        self.cbtn.pack(side=tk.RIGHT)

        self._sum = tk.Frame(card, bg=self.INFO_BG, padx=10, pady=8,
                             bd=1, relief=tk.SOLID)
        self._sum_l = tk.Label(self._sum, font=("Verdana", 8),
                               bg=self.INFO_BG, fg=self.INFO_FG,
                               justify=tk.LEFT, wraplength=520)
        self._sum_l.pack(anchor=tk.W)

        self._jc = tk.Frame(card, bg=self.JSON_BG)
        self._jc.pack(fill=tk.BOTH, expand=True)

        sy = ttk.Scrollbar(self._jc, orient=tk.VERTICAL)
        sy.pack(side=tk.RIGHT, fill=tk.Y)
        sx = ttk.Scrollbar(self._jc, orient=tk.HORIZONTAL)
        sx.pack(side=tk.BOTTOM, fill=tk.X)

        self.jout = tk.Text(
            self._jc, bg=self.JSON_BG, fg=self.JSON_FG,
            font=("Consolas", 9), wrap=tk.NONE, relief=tk.FLAT,
            padx=12, pady=12, insertbackground=self.JSON_FG,
            yscrollcommand=sy.set, xscrollcommand=sx.set)
        self.jout.insert("1.0", "No extraction output yet.")
        self.jout.configure(state=tk.DISABLED)
        self.jout.pack(fill=tk.BOTH, expand=True)
        sy.configure(command=self.jout.yview)
        sx.configure(command=self.jout.xview)

        self._cok = tk.Frame(card, bg=self.OK_BG, padx=10, pady=6,
                             bd=1, relief=tk.SOLID)
        tk.Label(self._cok, text="JSON successfully copied to clipboard.",
                 font=("Verdana", 9, "bold"),
                 bg=self.OK_BG, fg=self.OK_FG).pack(anchor=tk.W)

    # ── Actions ──────────────────────────────────────────────────

    def _browse_pdf(self):
        path = filedialog.askopenfilename(
            title="Select PDF File",
            filetypes=[("PDF Files", "*.pdf"), ("All Files", "*.*")])
        if not path:
            return
        self.pdf_path = path
        sz = os.path.getsize(path) / (1024 * 1024)
        self._fi_lbl.configure(
            text=f"Selected: {os.path.basename(path)} ({sz:.2f} MB)")
        self._fi.pack(fill=tk.X, pady=(8, 0))
        self._refresh_btn()

    def _key_changed(self):
        self.license_verified = False
        self._lm.pack_forget()
        self._refresh_btn()

    def _verify(self):
        key = self.key_entry.get().strip()
        if not key:
            self._lic_msg("Please enter a product key.", "error")
            return
        self.vbtn.configure(state=tk.DISABLED, text="Verifying...")
        self.root.update()
        threading.Thread(target=self._do_verify, args=(key,),
                         daemon=True).start()

    def _do_verify(self, key):
        if not check_internet():
            self.root.after(0, self._lic_msg,
                           "No internet connection detected.", "error")
            self.root.after(0, self._reset_vbtn)
            return

        result = validate_license(key)

        if result.get("valid"):
            self.license_verified = True
            self.root.after(0, self._lic_msg,
                           "Product key verified successfully. "
                           "PDF extraction is now enabled.", "success")
        elif key.strip().upper() in DEMO_KEYS:
            self.license_verified = True
            self.root.after(0, self._lic_msg,
                           "Product key verified successfully. "
                           "PDF extraction is now enabled.", "success")
        else:
            self.license_verified = False
            err = result.get("error") or "Invalid license key."
            self.root.after(0, self._lic_msg, err, "error")

        self.root.after(0, self._reset_vbtn)
        self.root.after(0, self._refresh_btn)

    def _reset_vbtn(self):
        self.vbtn.configure(state=tk.NORMAL, text="Verify")

    def _lic_msg(self, text, kind):
        bg = self.OK_BG if kind == "success" else self.ERR_BG
        fg = self.OK_FG if kind == "success" else self.ERR_FG
        self._lm.configure(bg=bg)
        self._ll.configure(text=text, bg=bg, fg=fg)
        self._lm.pack(fill=tk.X, pady=(8, 0))

    def _refresh_btn(self):
        ok = self.pdf_path and self.license_verified and not self.processing
        if ok:
            self.ebtn.configure(bg=self.ORANGE, state=tk.NORMAL,
                                cursor="hand2")
        else:
            self.ebtn.configure(bg=self.SLATE_300, state=tk.DISABLED,
                                cursor="arrow")

    def _extract(self):
        if self.processing or not self.pdf_path or not self.license_verified:
            return
        self.processing = True
        self._refresh_btn()
        self._pb["value"] = 0
        self._pf.pack(fill=tk.X, pady=(10, 0))
        self._sf.pack_forget()
        self._cok.pack_forget()
        self._sum.pack_forget()

        self.jout.configure(state=tk.NORMAL)
        self.jout.delete("1.0", tk.END)
        self.jout.insert("1.0", "Extracting PDF data...")
        self.jout.configure(state=tk.DISABLED)
        self.cbtn.configure(bg=self.SLATE_300, state=tk.DISABLED)

        threading.Thread(target=self._run, daemon=True).start()

    def _prog(self, text, pct):
        self._pt.configure(text=text)
        self._pp.configure(text=f"{pct}%")
        self._pb["value"] = pct

    def _run(self):
        try:
            self.root.after(0, self._prog, "Reading PDF file...", 10)
            time.sleep(0.4)

            jsel = self.jur_var.get()
            if jsel == "Auto Detect":
                self.root.after(0, self._prog,
                               "Detecting Jurisdiction...", 20)
                jurisdiction = detect_jurisdiction(self.pdf_path)
                time.sleep(0.3)
            else:
                jurisdiction = jsel.split(" - ")[0].strip()
                self.root.after(0, self._prog,
                               f"Jurisdiction: {jurisdiction}", 20)
                time.sleep(0.2)

            dsel = self.doc_var.get()
            tmap = {n: c for c, n in REPORT_TYPES}
            report_type = tmap.get(dsel, "auto")

            self.root.after(0, self._prog,
                           "Detecting Document Type...", 30)
            time.sleep(0.3)

            self.root.after(0, self._prog,
                           "Analysing PDF Structure...", 45)
            time.sleep(0.2)

            self.root.after(0, self._prog,
                           "Extracting Condition Report Data...", 60)

            result = extract_pdf(
                self.pdf_path,
                jurisdiction=jurisdiction,
                report_type=report_type,
                output_dir=os.path.dirname(self.pdf_path),
                save_images=True)

            self.root.after(0, self._prog,
                           "Validating Extracted Data...", 80)
            time.sleep(0.3)

            self.root.after(0, self._prog, "Building JSON...", 92)
            self.extracted_json = json.dumps(
                result, indent=2, ensure_ascii=False)
            time.sleep(0.2)

            self.root.after(0, self._prog, "Extraction Complete", 100)
            self.root.after(0, self._show_result, result)
            self.root.after(0, self._status,
                           "PDF extraction completed successfully.",
                           "success")
        except Exception as e:
            self.root.after(0, self._prog, "Extraction Failed", 0)
            self.root.after(0, self._status, f"Error: {e}", "error")
        finally:
            self.root.after(0, self._end_run)

    def _show_result(self, result):
        self.jout.configure(state=tk.NORMAL)
        self.jout.delete("1.0", tk.END)
        self.jout.insert("1.0", self.extracted_json)
        self.jout.configure(state=tk.DISABLED)

        self.cbtn.configure(bg=self.DARK, state=tk.NORMAL, cursor="hand2")

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

        empty_ct = sum(1 for a in areas
                       if not any(c.get("start_of_tenancy", {}).get("clean")
                                  for c in a.get("components", [])))
        warns = []
        if empty_ct:
            warns.append(f"{empty_ct} area(s) with no extracted data")

        lines = [
            f"Jurisdiction Detected:  {result.get('jurisdiction', 'N/A')}",
            f"Document Type Detected:  "
            f"{result.get('document_type', 'N/A')}",
            f"Pages Processed:  {meta.get('total_pages', 0)}",
            f"Property Areas Detected:  {len(areas)}",
            f"Condition Records Extracted:  {comps}",
            f"Validation Status:  "
            f"{'Passed' if not missing else 'Review Required'}",
        ]
        if warns:
            lines.append(f"Warnings:  {'; '.join(warns)}")
        if missing:
            lines.append(f"Missing Data:  {', '.join(missing)}")

        self._sum_l.configure(text="\n".join(lines))
        self._sum.pack(fill=tk.X, pady=(0, 6), before=self._jc)

    def _status(self, text, kind):
        bg = self.OK_BG if kind == "success" else self.ERR_BG
        fg = self.OK_FG if kind == "success" else self.ERR_FG
        self._sf.configure(bg=bg)
        self._sl.configure(text=text, bg=bg, fg=fg)
        self._sf.pack(fill=tk.X, pady=(8, 0))

    def _end_run(self):
        self.processing = False
        self._refresh_btn()

    def _copy_json(self):
        if not self.extracted_json:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(self.extracted_json)
        self._cok.pack(fill=tk.X, pady=(6, 0))
        self.root.after(3000, lambda: self._cok.pack_forget())


def run_gui():
    root = tk.Tk()
    app = ExtractorApp(root)
    root.mainloop()
