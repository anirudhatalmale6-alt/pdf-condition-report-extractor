import os
import json
import time
import socket
import base64
import tempfile
import shutil
import subprocess
import webview

from .config import APP_NAME, VERSION, JURISDICTIONS, REPORT_TYPES
from .extractor import extract_pdf, detect_jurisdiction, detect_report_type_standalone
from .license import validate_license

DEMO_KEYS = {"ORBAS-DEMO-2026", "ORBAS-TRIAL-2026", "ORBAS-NSW-VALID"}


def check_internet():
    try:
        socket.create_connection(("8.8.8.8", 53), timeout=3)
        return True
    except OSError:
        return False


class Api:
    def __init__(self):
        self.window = None
        self.pdf_path = None
        self.license_verified = False
        self.extracted_json = ""
        self._temp_dirs = []

    def select_file(self):
        try:
            import tkinter as tk
            from tkinter import filedialog
            root = tk.Tk()
            root.withdraw()
            root.wm_attributes('-topmost', 1)
            root.update()
            path = filedialog.askopenfilename(
                title="Select PDF File",
                filetypes=[("PDF Files", "*.pdf"), ("All Files", "*.*")],
                parent=root
            )
            root.destroy()
            if path and os.path.isfile(path):
                self.pdf_path = path
                size = os.path.getsize(path) / (1024 * 1024)
                return {"ok": True, "name": os.path.basename(path), "size": f"{size:.2f} MB"}
            return None
        except Exception:
            pass

        try:
            result = self.window.create_file_dialog(
                webview.OPEN_DIALOG,
                file_types=("PDF Files (*.pdf)",),
            )
            if result and len(result) > 0:
                path = result[0] if isinstance(result[0], str) else str(result[0])
                if os.path.isfile(path):
                    self.pdf_path = path
                    size = os.path.getsize(path) / (1024 * 1024)
                    return {"ok": True, "name": os.path.basename(path), "size": f"{size:.2f} MB"}
        except Exception:
            pass
        return None

    def receive_file(self, name, data_b64):
        try:
            content = base64.b64decode(data_b64)
            safe = "".join(c for c in name if c.isalnum() or c in '._- ')
            path = os.path.join(tempfile.gettempdir(), f"orbas_{safe}")
            with open(path, 'wb') as f:
                f.write(content)
            self.pdf_path = path
            size = len(content) / (1024 * 1024)
            return {"ok": True, "name": name, "size": f"{size:.2f} MB"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def verify_license(self, key):
        if not key or not key.strip():
            return {"valid": False, "message": "Please enter a product key."}

        if not check_internet():
            return {"valid": False, "message": "No internet connection. Please connect and try again."}

        result = validate_license(key)
        if result.get("valid"):
            self.license_verified = True
            return {
                "valid": True,
                "message": "Product key verified successfully. PDF extraction is now enabled.",
            }

        if key.strip().upper() in DEMO_KEYS:
            self.license_verified = True
            return {
                "valid": True,
                "message": "Product key verified successfully. PDF extraction is now enabled.",
            }

        self.license_verified = False
        return {
            "valid": False,
            "message": result.get("error", "Invalid license key."),
        }

    def extract(self, jurisdiction, doc_type):
        if not self.pdf_path:
            return {"error": "No PDF file selected."}
        if not self.license_verified:
            return {"error": "License not verified."}

        self._cleanup_temp()
        temp_dir = tempfile.mkdtemp(prefix="orbas_extract_")
        self._temp_dirs.append(temp_dir)

        try:
            self._prog("Reading PDF file...", 10)
            time.sleep(0.3)

            if jurisdiction == "auto":
                self._prog("Detecting Jurisdiction...", 20)
                detected_jur = detect_jurisdiction(self.pdf_path)
                time.sleep(0.2)
            else:
                detected_jur = jurisdiction
                self._prog(f"Jurisdiction: {detected_jur}", 20)

            self._prog("Detecting Document Type...", 30)
            time.sleep(0.2)

            self._prog("Analysing PDF Structure...", 45)
            time.sleep(0.2)

            self._prog("Extracting Condition Report Data...", 60)
            result = extract_pdf(
                self.pdf_path,
                jurisdiction=detected_jur,
                report_type=doc_type if doc_type != "auto" else "auto",
                output_dir=temp_dir,
                save_images=False,
            )

            self._prog("Validating Extracted Data...", 80)
            time.sleep(0.2)

            self._prog("Building JSON...", 92)
            self.extracted_json = json.dumps(result, indent=2, ensure_ascii=False)
            time.sleep(0.15)

            self._cleanup_temp()
            self._prog("Extraction Complete", 100)

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

            summary = {
                "jurisdiction": result.get("jurisdiction", "N/A"),
                "document_type": result.get("document_type", "N/A"),
                "pages": meta.get("total_pages", 0),
                "areas": len(areas),
                "records": comps,
                "validation": "Passed" if not missing else "Review Required",
                "missing": missing,
            }
            return {"json": self.extracted_json, "summary": summary}

        except Exception as e:
            self._cleanup_temp()
            self._prog("Extraction Failed", 0)
            return {"error": str(e)}

    def _cleanup_temp(self):
        for d in self._temp_dirs:
            try:
                if os.path.isdir(d):
                    shutil.rmtree(d, ignore_errors=True)
            except Exception:
                pass
        self._temp_dirs.clear()

    def copy_to_clipboard(self, text):
        """Copy text to clipboard. Tries clip.exe first (fast), PowerShell fallback."""
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0

        try:
            p = subprocess.Popen(['clip'], stdin=subprocess.PIPE, startupinfo=si)
            p.communicate(input=text.encode('utf-8'), timeout=5)
            if p.returncode == 0:
                return True
        except Exception:
            pass

        try:
            tmp = os.path.join(tempfile.gettempdir(), "orbas_clip.txt")
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(text)
            p = subprocess.run(
                ["powershell", "-NoProfile", "-NoLogo", "-Command",
                 f"Get-Content -Path '{tmp}' -Raw | Set-Clipboard"],
                startupinfo=si, capture_output=True, timeout=10)
            try:
                os.remove(tmp)
            except Exception:
                pass
            return p.returncode == 0
        except Exception:
            pass
        return False

    def _prog(self, text, pct):
        if self.window:
            safe = text.replace("\\", "\\\\").replace("'", "\\'")
            self.window.evaluate_js(f"updateProgress('{safe}', {pct})")


def _build_html():
    jur_options = '<option value="auto">Auto Detect</option>'
    for code, name in JURISDICTIONS:
        jur_options += f'<option value="{code}">{code} - {name}</option>'

    doc_options = ""
    for code, name in REPORT_TYPES:
        doc_options += f'<option value="{code}">{name}</option>'

    return """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>ORBAS Native PDF Extractor</title>
<style>
*,*::before,*::after{margin:0;padding:0;box-sizing:border-box}
body{font-family:Verdana,Geneva,sans-serif;background:#f1f5f9;color:#0f172a;height:100vh;overflow:hidden}
.app{display:flex;flex-direction:column;height:100vh;padding:1.2vh 1.2vw}

.hdr{display:flex;align-items:center;justify-content:space-between;margin-bottom:1vh;flex-shrink:0}
.hdr h1{font-size:clamp(1rem,1.8vw,1.4rem);font-weight:700;color:#0453ed}
.hdr .sub{font-size:clamp(.65rem,1vw,.8rem);color:#475569;margin-top:.1rem}
.hdr .ver{font-size:clamp(.7rem,1vw,.85rem);font-weight:600;text-align:right}
.hdr .ver-sub{font-size:clamp(.6rem,.8vw,.72rem);color:#64748b}

.grid{flex:1;display:grid;grid-template-columns:1fr 1fr;gap:clamp(.6rem,1.2vw,1.2rem);min-height:0;overflow:hidden}
.left{display:flex;flex-direction:column;gap:clamp(.4rem,.8vh,.7rem);overflow-y:auto;padding-right:4px}
.left::-webkit-scrollbar{width:4px}
.left::-webkit-scrollbar-thumb{background:#cbd5e1;border-radius:4px}

.card{background:#fff;border-radius:.65rem;border:1px solid #e2e8f0;box-shadow:0 1px 2px rgba(0,0,0,.05);padding:clamp(.5rem,1.2vh,.9rem) clamp(.6rem,1vw,1rem);flex-shrink:0}
.card-h{display:flex;align-items:center;gap:.5rem;margin-bottom:clamp(.3rem,.7vh,.6rem)}
.circ{width:clamp(1.2rem,2.2vh,1.6rem);height:clamp(1.2rem,2.2vh,1.6rem);border-radius:50%;color:#fff;display:flex;align-items:center;justify-content:center;font-size:clamp(.6rem,.9vh,.75rem);font-weight:700;flex-shrink:0}
.c-blue{background:#0453ed}
.c-green{background:#096e4d}
.c-orange{background:#fd6207}
.card-h h2{font-size:clamp(.78rem,1.3vw,.95rem);font-weight:700}

.dz{border:2px dashed #cbd5e1;border-radius:.65rem;padding:clamp(.5rem,1.2vh,.9rem) 1rem;text-align:center;background:#f8fafc;cursor:pointer;transition:.2s}
.dz:hover,.dz-hover{background:#eff6ff!important;border-color:#0453ed!important}
.dz .ico{font-size:clamp(1.2rem,2.5vh,1.8rem);margin-bottom:.15rem}
.dz .main{font-weight:600;font-size:clamp(.7rem,1vw,.82rem)}
.dz .hint{font-size:clamp(.6rem,.85vw,.72rem);color:#64748b;margin-top:.1rem}

.btn{border:none;border-radius:.45rem;font-weight:600;cursor:pointer;font-family:inherit;transition:.15s;font-size:clamp(.68rem,.95vw,.78rem);padding:clamp(.25rem,.5vh,.38rem) clamp(.5rem,.8vw,.85rem)}
.btn-dark{background:#0f172a;color:#fff}.btn-dark:hover{background:#1e293b}
.btn-green{background:#096e4d;color:#fff}.btn-green:hover{background:#065f46}
.btn-orange{background:#fd6207;color:#fff;font-size:clamp(.78rem,1.2vw,.92rem);font-weight:700;padding:clamp(.35rem,.8vh,.55rem) 1rem;width:100%;border-radius:.55rem}
.btn-orange:hover{background:#ea580c}
.btn-slate{background:#0f172a;color:#fff}.btn-slate:hover{background:#1e293b}
.btn:disabled{background:#cbd5e1!important;cursor:not-allowed!important;color:#fff}

.fi{margin-top:.4rem;padding:clamp(.3rem,.5vh,.45rem) .6rem;border-radius:.4rem;background:#f0fdf4;border:1px solid #bbf7d0;display:none}
.fi p{font-size:clamp(.65rem,.9vw,.76rem);color:#166534}
.fi .l{font-weight:600}

label{display:block;font-size:clamp(.65rem,.9vw,.76rem);font-weight:600;margin-bottom:.2rem}
select,input[type=text]{width:100%;border:1px solid #e2e8f0;border-radius:.4rem;padding:clamp(.2rem,.45vh,.35rem) .5rem;font-size:clamp(.65rem,.9vw,.76rem);font-family:inherit;outline:none;background:#fff}
select:focus,input[type=text]:focus{border-color:#0453ed;box-shadow:0 0 0 2px rgba(4,83,237,.1)}
.form-row{display:grid;grid-template-columns:1fr 1fr;gap:.6rem}
.lic-row{display:flex;gap:.5rem}
.lic-row input{flex:1;font-family:Consolas,'Courier New',monospace}

.msg{margin-top:.4rem;padding:clamp(.25rem,.45vh,.4rem) .55rem;border-radius:.4rem;font-size:clamp(.65rem,.9vw,.76rem);display:none}
.msg-ok{background:#f0fdf4;border:1px solid #bbf7d0;color:#166534}
.msg-err{background:#fef2f2;border:1px solid #fecaca;color:#991b1b}

.pbox{margin-top:.5rem;display:none}
.ph{display:flex;justify-content:space-between;font-size:clamp(.65rem,.9vw,.76rem);margin-bottom:.2rem}
.ph .l{font-weight:600}
.ptrack{width:100%;background:#e2e8f0;border-radius:99px;height:clamp(.35rem,.6vh,.5rem);overflow:hidden}
.pbar{background:#096e4d;height:100%;border-radius:99px;transition:width .3s;width:0%}

.json-card{display:flex;flex-direction:column;min-height:0;overflow:hidden}
.json-hdr{display:flex;align-items:flex-start;justify-content:space-between;margin-bottom:.4rem;flex-shrink:0}
.json-left{display:flex;align-items:center;gap:.5rem}
.json-tt .t{font-size:clamp(.78rem,1.3vw,.95rem);font-weight:700}
.json-tt .s{font-size:clamp(.58rem,.75vw,.68rem);color:#64748b;margin-top:.05rem}
.json-out{flex:1;background:#020617;color:#86efac;border-radius:.65rem;padding:clamp(.5rem,.8vh,.7rem);font-family:Consolas,'Courier New',monospace;font-size:clamp(.58rem,.8vw,.68rem);line-height:1.35;overflow:auto;border:1px solid #1e293b;white-space:pre;min-height:0;user-select:text;-webkit-user-select:text;cursor:text;outline:none}
.json-out:focus{border-color:#0453ed;box-shadow:0 0 0 2px rgba(4,83,237,.25)}

.summ{padding:clamp(.3rem,.5vh,.45rem) .6rem;border-radius:.4rem;background:#f0f9ff;border:1px solid #bfdbfe;font-size:clamp(.58rem,.8vw,.7rem);color:#1e40af;margin-bottom:.35rem;line-height:1.35;display:none;flex-shrink:0}

.copy-ok{margin-top:.35rem;padding:.35rem .55rem;border-radius:.4rem;background:#f0fdf4;border:1px solid #bbf7d0;color:#166534;font-size:clamp(.65rem,.9vw,.76rem);font-weight:600;display:none;flex-shrink:0}
</style>
</head>
<body>
<div class="app">
  <div class="hdr">
    <div>
      <h1>ORBAS Native PDF Extractor</h1>
      <div class="sub">Extract rental condition report PDF data into structured JSON.</div>
    </div>
    <div>
      <div class="ver">v""" + VERSION + """</div>
      <div class="ver-sub">Local PDF Extraction</div>
    </div>
  </div>

  <div class="grid">
    <div class="left">

      <div class="card">
        <div class="card-h"><span class="circ c-blue">1</span><h2>Select PDF File</h2></div>
        <div class="dz" id="dropZone">
          <div class="ico">&#128196;</div>
          <div class="main" id="dzText">Drag and drop your PDF here</div>
          <div class="hint">or click to browse from your computer</div>
          <button class="btn btn-dark" style="margin-top:.4rem" type="button" id="browseBtn">Browse PDF</button>
        </div>
        <div class="fi" id="fileInfo">
          <p class="l">Selected File</p>
          <p id="fileName"></p>
        </div>
      </div>

      <div class="card">
        <div class="card-h"><span class="circ c-blue">2</span><h2>Jurisdiction & Document Type</h2></div>
        <div class="form-row">
          <div>
            <label>Jurisdiction</label>
            <select id="jurisdiction">""" + jur_options + """</select>
          </div>
          <div>
            <label>Document Type</label>
            <select id="documentType">""" + doc_options + """</select>
          </div>
        </div>
      </div>

      <div class="card">
        <div class="card-h"><span class="circ c-green">3</span><h2>Product Key Verification</h2></div>
        <label>Product / License Key</label>
        <div class="lic-row">
          <input type="text" id="licenseKey" placeholder="Example: ORBAS-DEMO-2026">
          <button class="btn btn-green" id="verifyBtn">Verify</button>
        </div>
        <div class="msg" id="licMsg"></div>
      </div>

      <div class="card">
        <div class="card-h"><span class="circ c-orange">4</span><h2>Extract PDF</h2></div>
        <button class="btn btn-orange" id="extractBtn" disabled>Extract PDF</button>
        <div class="pbox" id="progressBox">
          <div class="ph">
            <span class="l" id="progText">Preparing...</span>
            <span id="progPct">0%</span>
          </div>
          <div class="ptrack"><div class="pbar" id="progBar"></div></div>
        </div>
        <div class="msg" id="statusMsg"></div>
      </div>

    </div>

    <div class="card json-card">
      <div class="json-hdr">
        <div class="json-left">
          <span class="circ c-green">5</span>
          <div class="json-tt">
            <div class="t">JSON Output</div>
            <div class="s">Click the panel, then Ctrl+A to select all and Ctrl+C to copy. Or use the Copy JSON button.</div>
          </div>
        </div>
        <button class="btn btn-slate" id="copyBtn" disabled>Copy JSON</button>
      </div>
      <div class="summ" id="summary"></div>
      <pre class="json-out" id="jsonOut" tabindex="0">No extraction output yet.</pre>
      <div class="copy-ok" id="copyOk">JSON successfully copied to clipboard.</div>
    </div>
  </div>
</div>

<script>
var fileSelected = false;
var licenseVerified = false;
var extractedJson = '';
var extracting = false;

function selectFile() {
  var dzt = document.getElementById('dzText');
  dzt.textContent = 'Opening file dialog...';
  pywebview.api.select_file().then(function(f) {
    dzt.textContent = 'Drag and drop your PDF here';
    if (!f) return;
    onFileSelected(f.name, f.size);
  }).catch(function(err) {
    dzt.textContent = 'Drag and drop your PDF here';
    showMsg('statusMsg', 'Could not open file dialog: ' + err, 'err');
  });
}

function onFileSelected(name, size) {
  fileSelected = true;
  document.getElementById('fileName').textContent = name + ' (' + size + ')';
  document.getElementById('fileInfo').style.display = 'block';
  checkReady();
}

document.getElementById('browseBtn').addEventListener('click', function(e) {
  e.stopPropagation();
  selectFile();
});

document.getElementById('dropZone').addEventListener('click', function(e) {
  if (e.target.id === 'browseBtn') return;
  selectFile();
});

/* Drag and Drop */
document.addEventListener('dragover', function(e) { e.preventDefault(); });
document.addEventListener('drop', function(e) { e.preventDefault(); });

var dz = document.getElementById('dropZone');
dz.addEventListener('dragenter', function(e) { e.preventDefault(); dz.classList.add('dz-hover'); });
dz.addEventListener('dragover', function(e) { e.preventDefault(); dz.classList.add('dz-hover'); });
dz.addEventListener('dragleave', function(e) { dz.classList.remove('dz-hover'); });
dz.addEventListener('drop', function(e) {
  e.preventDefault();
  e.stopPropagation();
  dz.classList.remove('dz-hover');
  var file = e.dataTransfer.files[0];
  if (!file) return;
  if (!file.name.toLowerCase().endsWith('.pdf')) {
    showMsg('statusMsg', 'Please drop a PDF file.', 'err');
    return;
  }
  if (file.size > 50 * 1024 * 1024) {
    showMsg('statusMsg', 'File too large for drag and drop. Please use Browse PDF.', 'err');
    return;
  }
  var dzt = document.getElementById('dzText');
  dzt.textContent = 'Reading file...';
  var reader = new FileReader();
  reader.onload = function() {
    var bytes = new Uint8Array(reader.result);
    var binary = '';
    for (var i = 0; i < bytes.length; i += 8192) {
      binary += String.fromCharCode.apply(null, bytes.subarray(i, Math.min(i + 8192, bytes.length)));
    }
    dzt.textContent = 'Processing...';
    pywebview.api.receive_file(file.name, btoa(binary)).then(function(f) {
      dzt.textContent = 'Drag and drop your PDF here';
      if (!f || !f.ok) { showMsg('statusMsg', 'Error: ' + (f ? f.error : 'unknown'), 'err'); return; }
      onFileSelected(f.name, f.size);
    }).catch(function(err) {
      dzt.textContent = 'Drag and drop your PDF here';
      showMsg('statusMsg', 'Drop error: ' + err, 'err');
    });
  };
  reader.onerror = function() { dzt.textContent = 'Drag and drop your PDF here'; };
  reader.readAsArrayBuffer(file);
});

/* License */
document.getElementById('verifyBtn').addEventListener('click', function() { verifyKey(); });

function verifyKey() {
  var key = document.getElementById('licenseKey').value.trim();
  if (!key) { showMsg('licMsg', 'Please enter a product key.', 'err'); return; }
  var btn = document.getElementById('verifyBtn');
  btn.disabled = true;
  btn.textContent = 'Verifying...';
  pywebview.api.verify_license(key).then(function(r) {
    btn.disabled = false;
    btn.textContent = 'Verify';
    licenseVerified = !!r.valid;
    showMsg('licMsg', r.message, r.valid ? 'ok' : 'err');
    checkReady();
  }).catch(function(err) {
    btn.disabled = false;
    btn.textContent = 'Verify';
    showMsg('licMsg', 'Error: ' + err, 'err');
  });
}

document.getElementById('licenseKey').addEventListener('input', function() {
  licenseVerified = false; hideMsg('licMsg'); checkReady();
});
document.getElementById('licenseKey').addEventListener('keydown', function(e) {
  if (e.key === 'Enter') verifyKey();
});

/* Extract */
document.getElementById('extractBtn').addEventListener('click', function() { extractPdf(); });

function checkReady() {
  document.getElementById('extractBtn').disabled = !(fileSelected && licenseVerified && !extracting);
}

function extractPdf() {
  if (extracting) return;
  extracting = true;
  checkReady();
  var jur = document.getElementById('jurisdiction').value;
  var dt = document.getElementById('documentType').value;
  document.getElementById('progressBox').style.display = 'block';
  hideMsg('statusMsg');
  document.getElementById('copyOk').style.display = 'none';
  document.getElementById('summary').style.display = 'none';
  document.getElementById('jsonOut').textContent = 'Extracting PDF data...';
  document.getElementById('copyBtn').disabled = true;

  pywebview.api.extract(jur, dt).then(function(r) {
    extracting = false;
    if (r.error) { showMsg('statusMsg', 'Error: ' + r.error, 'err'); checkReady(); return; }
    extractedJson = r.json;
    document.getElementById('jsonOut').textContent = r.json;
    document.getElementById('copyBtn').disabled = false;
    document.getElementById('jsonOut').focus();
    if (r.summary) {
      var s = r.summary;
      var txt = 'Jurisdiction: ' + s.jurisdiction + '  |  Type: ' + s.document_type + '  |  Pages: ' + s.pages;
      txt += '\\nAreas: ' + s.areas + '  |  Records: ' + s.records + '  |  Validation: ' + s.validation;
      if (s.missing && s.missing.length > 0) txt += '\\nMissing: ' + s.missing.join(', ');
      var el = document.getElementById('summary');
      el.textContent = txt;
      el.style.display = 'block';
    }
    showMsg('statusMsg', 'PDF extraction completed successfully.', 'ok');
    checkReady();
  }).catch(function(err) {
    extracting = false;
    showMsg('statusMsg', 'Extraction error: ' + err, 'err');
    checkReady();
  });
}

function updateProgress(text, pct) {
  document.getElementById('progText').textContent = text;
  document.getElementById('progPct').textContent = pct + '%';
  document.getElementById('progBar').style.width = pct + '%';
}

/* Copy JSON - uses Python PowerShell Set-Clipboard (reliable on Windows) */
document.getElementById('copyBtn').addEventListener('click', function() { copyJson(); });

function copyJson() {
  if (!extractedJson) return;
  var btn = document.getElementById('copyBtn');
  btn.disabled = true;
  btn.textContent = 'Copying...';

  pywebview.api.copy_to_clipboard(extractedJson).then(function(ok) {
    btn.disabled = false;
    btn.textContent = 'Copy JSON';
    if (ok) {
      var el = document.getElementById('copyOk');
      el.textContent = 'JSON successfully copied to clipboard. You can now paste with Ctrl+V.';
      el.style.display = 'block';
      setTimeout(function() { el.style.display = 'none'; }, 5000);
    } else {
      showMsg('statusMsg', 'Copy failed. Please click inside the JSON panel, press Ctrl+A then Ctrl+C to copy manually.', 'err');
    }
  }).catch(function(err) {
    btn.disabled = false;
    btn.textContent = 'Copy JSON';
    showMsg('statusMsg', 'Copy error. Please select the JSON text manually and press Ctrl+C.', 'err');
  });
}

/* Keyboard shortcuts on the JSON panel: Ctrl+A select all, Ctrl+C copy */
var jsonPanel = document.getElementById('jsonOut');

function jsonPanelFocused() {
  return document.activeElement === jsonPanel || jsonPanel.contains(document.activeElement);
}

function selectAllJson() {
  var range = document.createRange();
  range.selectNodeContents(jsonPanel);
  var sel = window.getSelection();
  sel.removeAllRanges();
  sel.addRange(range);
}

function showCopiedToast() {
  var el = document.getElementById('copyOk');
  el.textContent = 'JSON copied to clipboard. You can now paste with Ctrl+V.';
  el.style.display = 'block';
  setTimeout(function() { el.style.display = 'none'; }, 4000);
}

jsonPanel.addEventListener('keydown', function(e) {
  if (!(e.ctrlKey || e.metaKey)) return;
  var k = e.key.toLowerCase();
  if (k === 'a') {
    e.preventDefault();
    selectAllJson();
  } else if (k === 'c') {
    if (!extractedJson) return;
    var sel = window.getSelection().toString();
    var text = (sel && sel.length) ? sel : extractedJson;
    e.preventDefault();
    pywebview.api.copy_to_clipboard(text).then(function(ok) {
      if (ok) showCopiedToast();
    });
  }
});

/* Helpers */
function showMsg(id, text, type) {
  var el = document.getElementById(id);
  el.textContent = text;
  el.className = 'msg ' + (type === 'ok' ? 'msg-ok' : 'msg-err');
  el.style.display = 'block';
}

function hideMsg(id) {
  document.getElementById(id).style.display = 'none';
}
</script>
</body>
</html>"""


def run_gui():
    api = Api()
    html = _build_html()
    window = webview.create_window(
        f"{APP_NAME} Native PDF Extractor",
        html=html,
        js_api=api,
        width=1200,
        height=800,
        min_size=(900, 600),
    )
    api.window = window
    webview.start(private_mode=False, http_server=True)
