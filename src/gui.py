import os
import json
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext

from .config import APP_NAME, VERSION, JURISDICTIONS, REPORT_TYPES
from .extractor import extract_pdf
from .cloud_sync import CloudSync


class ExtractorApp:
    def __init__(self, root):
        self.root = root
        self.root.title(f"{APP_NAME} v{VERSION}")
        self.root.geometry("800x650")
        self.root.minsize(700, 550)
        self.root.configure(bg="#f0f0f0")

        self.pdf_path = tk.StringVar()
        self.jurisdiction = tk.StringVar(value="NSW")
        self.report_type = tk.StringVar(value="auto")
        self.endpoint_url = tk.StringVar()
        self.api_key = tk.StringVar()
        self.output_dir = tk.StringVar()
        self.processing = False

        self._build_ui()

    def _build_ui(self):
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("Title.TLabel", font=("Segoe UI", 14, "bold"), background="#f0f0f0")
        style.configure("TButton", padding=6)
        style.configure("Extract.TButton", padding=10, font=("Segoe UI", 10, "bold"))

        main_frame = ttk.Frame(self.root, padding=15)
        main_frame.pack(fill=tk.BOTH, expand=True)

        ttk.Label(main_frame, text=APP_NAME, style="Title.TLabel").pack(anchor=tk.W)
        ttk.Separator(main_frame, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=(5, 10))

        file_frame = ttk.LabelFrame(main_frame, text="PDF File", padding=10)
        file_frame.pack(fill=tk.X, pady=(0, 8))

        file_inner = ttk.Frame(file_frame)
        file_inner.pack(fill=tk.X)

        self.file_entry = ttk.Entry(file_inner, textvariable=self.pdf_path, state="readonly")
        self.file_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8))

        ttk.Button(file_inner, text="Browse...", command=self._browse_pdf).pack(side=tk.LEFT)

        self.drop_label = ttk.Label(
            file_frame,
            text="Drag and drop a PDF file here, or click Browse",
            foreground="gray",
            anchor=tk.CENTER,
        )
        self.drop_label.pack(fill=tk.X, pady=(5, 0))

        settings_frame = ttk.LabelFrame(main_frame, text="Settings", padding=10)
        settings_frame.pack(fill=tk.X, pady=(0, 8))

        row1 = ttk.Frame(settings_frame)
        row1.pack(fill=tk.X, pady=(0, 5))

        ttk.Label(row1, text="Jurisdiction:").pack(side=tk.LEFT, padx=(0, 5))
        jurisdiction_combo = ttk.Combobox(
            row1,
            textvariable=self.jurisdiction,
            values=[f"{code} - {name}" for code, name in JURISDICTIONS],
            state="readonly",
            width=35,
        )
        jurisdiction_combo.pack(side=tk.LEFT, padx=(0, 20))
        jurisdiction_combo.set("NSW - New South Wales")
        jurisdiction_combo.bind("<<ComboboxSelected>>", self._on_jurisdiction_change)

        ttk.Label(row1, text="Report Type:").pack(side=tk.LEFT, padx=(0, 5))
        report_combo = ttk.Combobox(
            row1,
            textvariable=self.report_type,
            values=[f"{code} - {name}" for code, name in REPORT_TYPES],
            state="readonly",
            width=35,
        )
        report_combo.pack(side=tk.LEFT)
        report_combo.set("auto - Auto Detect")
        report_combo.bind("<<ComboboxSelected>>", self._on_report_type_change)

        row2 = ttk.Frame(settings_frame)
        row2.pack(fill=tk.X, pady=(0, 5))

        ttk.Label(row2, text="Output Folder:").pack(side=tk.LEFT, padx=(0, 5))
        ttk.Entry(row2, textvariable=self.output_dir).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8))
        ttk.Button(row2, text="Browse...", command=self._browse_output).pack(side=tk.LEFT)

        cloud_frame = ttk.LabelFrame(main_frame, text="Cloud Sync (Optional)", padding=10)
        cloud_frame.pack(fill=tk.X, pady=(0, 8))

        row3 = ttk.Frame(cloud_frame)
        row3.pack(fill=tk.X, pady=(0, 5))
        ttk.Label(row3, text="Endpoint URL:").pack(side=tk.LEFT, padx=(0, 5))
        ttk.Entry(row3, textvariable=self.endpoint_url).pack(side=tk.LEFT, fill=tk.X, expand=True)

        row4 = ttk.Frame(cloud_frame)
        row4.pack(fill=tk.X)
        ttk.Label(row4, text="API Key:").pack(side=tk.LEFT, padx=(0, 5))
        ttk.Entry(row4, textvariable=self.api_key, show="*").pack(side=tk.LEFT, fill=tk.X, expand=True)

        btn_frame = ttk.Frame(main_frame)
        btn_frame.pack(fill=tk.X, pady=(0, 8))

        self.extract_btn = ttk.Button(
            btn_frame, text="Extract PDF", command=self._start_extraction, style="Extract.TButton"
        )
        self.extract_btn.pack(side=tk.LEFT, padx=(0, 10))

        self.progress = ttk.Progressbar(btn_frame, mode="indeterminate", length=200)
        self.progress.pack(side=tk.LEFT, fill=tk.X, expand=True)

        log_frame = ttk.LabelFrame(main_frame, text="Output Log", padding=5)
        log_frame.pack(fill=tk.BOTH, expand=True)

        self.log_text = scrolledtext.ScrolledText(log_frame, height=10, font=("Consolas", 9), state="disabled")
        self.log_text.pack(fill=tk.BOTH, expand=True)

    def _on_jurisdiction_change(self, event):
        value = event.widget.get()
        code = value.split(" - ")[0].strip()
        self.jurisdiction.set(code)

    def _on_report_type_change(self, event):
        value = event.widget.get()
        code = value.split(" - ")[0].strip()
        self.report_type.set(code)

    def _browse_pdf(self):
        path = filedialog.askopenfilename(
            title="Select PDF File",
            filetypes=[("PDF Files", "*.pdf"), ("All Files", "*.*")],
        )
        if path:
            self.pdf_path.set(path)
            self.drop_label.configure(text=os.path.basename(path), foreground="black")
            if not self.output_dir.get():
                self.output_dir.set(os.path.dirname(path))

    def _browse_output(self):
        path = filedialog.askdirectory(title="Select Output Folder")
        if path:
            self.output_dir.set(path)

    def _log(self, message):
        self.log_text.configure(state="normal")
        self.log_text.insert(tk.END, message + "\n")
        self.log_text.see(tk.END)
        self.log_text.configure(state="disabled")

    def _start_extraction(self):
        if self.processing:
            return

        pdf_path = self.pdf_path.get()
        if not pdf_path or not os.path.isfile(pdf_path):
            messagebox.showerror("Error", "Please select a valid PDF file.")
            return

        output_dir = self.output_dir.get()
        if not output_dir:
            output_dir = os.path.dirname(pdf_path)
            self.output_dir.set(output_dir)

        os.makedirs(output_dir, exist_ok=True)

        self.processing = True
        self.extract_btn.configure(state="disabled")
        self.progress.start(10)
        self._log("=" * 50)
        self._log(f"Starting extraction: {os.path.basename(pdf_path)}")
        self._log(f"Jurisdiction: {self.jurisdiction.get()}")
        self._log(f"Report Type: {self.report_type.get()}")

        thread = threading.Thread(target=self._run_extraction, args=(pdf_path, output_dir), daemon=True)
        thread.start()

    def _run_extraction(self, pdf_path, output_dir):
        try:
            self.root.after(0, self._log, "Extracting PDF content...")

            result = extract_pdf(
                pdf_path,
                jurisdiction=self.jurisdiction.get(),
                report_type=self.report_type.get(),
                output_dir=output_dir,
                save_images=True,
            )

            base_name = os.path.splitext(os.path.basename(pdf_path))[0]
            json_path = os.path.join(output_dir, f"{base_name}_extracted.json")

            with open(json_path, "w", encoding="utf-8") as f:
                json.dump(result, f, indent=2, ensure_ascii=False)

            room_count = len(result.get("rooms", []))
            item_count = sum(len(r.get("items", [])) for r in result.get("rooms", []))
            image_count = len(result.get("images", []))

            self.root.after(0, self._log, f"Extraction complete!")
            self.root.after(0, self._log, f"  Rooms found: {room_count}")
            self.root.after(0, self._log, f"  Items extracted: {item_count}")
            self.root.after(0, self._log, f"  Images extracted: {image_count}")
            self.root.after(0, self._log, f"  JSON saved to: {json_path}")

            if result.get("metadata", {}).get("detected_report_type"):
                self.root.after(0, self._log,
                    f"  Detected report type: {result['metadata']['detected_report_type']}")

            endpoint = self.endpoint_url.get().strip()
            if endpoint:
                self.root.after(0, self._log, "Syncing to cloud endpoint...")
                sync = CloudSync(
                    endpoint_url=endpoint,
                    api_key=self.api_key.get().strip() or None,
                )
                sync_result = sync.sync(result, on_progress=lambda msg: self.root.after(0, self._log, f"  {msg}"))
                if sync_result["success"]:
                    self.root.after(0, self._log,
                        f"  Cloud sync successful! (status {sync_result['status_code']})")
                else:
                    self.root.after(0, self._log, f"  Cloud sync failed: {sync_result['error']}")
            else:
                self.root.after(0, self._log, "  Cloud sync skipped (no endpoint configured)")

            self.root.after(0, self._log, "Done!")

        except Exception as e:
            self.root.after(0, self._log, f"ERROR: {str(e)}")
            self.root.after(0, messagebox.showerror, "Extraction Error", str(e))
        finally:
            self.root.after(0, self._finish_extraction)

    def _finish_extraction(self):
        self.processing = False
        self.extract_btn.configure(state="normal")
        self.progress.stop()


def run_gui():
    root = tk.Tk()
    app = ExtractorApp(root)
    root.mainloop()
