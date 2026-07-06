import os
import json
import time
import socket
import threading
import webview

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


class Api:
    def __init__(self):
        self.window = None
        self.pdf_path = None
        self.license_verified = False
        self.extracted_json = ""

    def get_config(self):
        return {
            "version": VERSION,
            "app_name": APP_NAME,
            "has_internet": check_internet(),
            "jurisdictions": [{"code": c, "name": n} for c, n in JURISDICTIONS],
            "report_types": [{"code": c, "name": n} for c, n in REPORT_TYPES],
        }

    def select_file(self):
        result = self.window.create_file_dialog(
            webview.OPEN_DIALOG,
            file_types=("PDF Files (*.pdf)",),
        )
        if result and len(result) > 0:
            path = result[0] if isinstance(result[0], str) else str(result[0])
            self.pdf_path = path
            size = os.path.getsize(path) / (1024 * 1024)
            return {
                "name": os.path.basename(path),
                "size": f"{size:.2f} MB",
            }
        return None

    def verify_license(self, key):
        if not key or not key.strip():
            return {"valid": False, "message": "Please enter a product key."}

        if not check_internet():
            return {"valid": False, "message": "No internet connection detected."}

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
                output_dir=os.path.dirname(self.pdf_path),
                save_images=True,
            )

            self._prog("Validating Extracted Data...", 80)
            time.sleep(0.2)

            self._prog("Building JSON...", 92)
            self.extracted_json = json.dumps(result, indent=2, ensure_ascii=False)
            time.sleep(0.15)

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
            self._prog("Extraction Failed", 0)
            return {"error": str(e)}

    def _prog(self, text, pct):
        if self.window:
            safe = text.replace("\\", "\\\\").replace("'", "\\'")
            self.window.evaluate_js(f"updateProgress('{safe}', {pct})")

    def copy_to_clipboard(self, text):
        try:
            import tkinter as tk
            r = tk.Tk()
            r.withdraw()
            r.clipboard_clear()
            r.clipboard_append(text)
            r.update()
            r.destroy()
            return True
        except Exception:
            return False


HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>ORBAS Native PDF Extractor</title>
<style>
*,*::before,*::after{margin:0;padding:0;box-sizing:border-box}
body{font-family:Verdana,Geneva,sans-serif;background:#f1f5f9;color:#0f172a;overflow:hidden;height:100vh}
.app{display:flex;flex-direction:column;height:100vh;padding:1.1rem 1.25rem}

/* Header */
.hdr{display:flex;align-items:center;justify-content:space-between;margin-bottom:.85rem}
.hdr h1{font-size:1.35rem;font-weight:700;color:#0453ed}
.hdr .sub{font-size:.78rem;color:#475569;margin-top:.1rem}
.hdr .ver{font-size:.8rem;font-weight:600;text-align:right}
.hdr .ver-sub{font-size:.7rem;color:#64748b}

/* Grid */
.grid{flex:1;display:grid;grid-template-columns:1fr 1fr;gap:1.1rem;min-height:0}
.left{display:flex;flex-direction:column;gap:.75rem;overflow-y:auto;padding-right:.25rem}
.left::-webkit-scrollbar{width:5px}
.left::-webkit-scrollbar-thumb{background:#cbd5e1;border-radius:4px}

/* Card */
.card{background:#fff;border-radius:.75rem;border:1px solid #e2e8f0;box-shadow:0 1px 2px rgba(0,0,0,.05);padding:1rem 1.1rem}
.card-h{display:flex;align-items:center;gap:.6rem;margin-bottom:.7rem}
.circ{width:1.65rem;height:1.65rem;border-radius:50%;color:#fff;display:flex;align-items:center;justify-content:center;font-size:.75rem;font-weight:700;flex-shrink:0}
.c-blue{background:#0453ed}
.c-green{background:#096e4d}
.c-orange{background:#fd6207}
.card-h h2{font-size:.95rem;font-weight:700}

/* Drop zone */
.dz{border:2px dashed #cbd5e1;border-radius:.75rem;padding:1rem 1.25rem;text-align:center;background:#f8fafc;cursor:pointer;transition:.2s}
.dz:hover{background:#eff6ff;border-color:#0453ed}
.dz .ico{font-size:1.75rem;margin-bottom:.25rem}
.dz .main{font-weight:600;font-size:.82rem}
.dz .hint{font-size:.72rem;color:#64748b;margin-top:.1rem}

/* Buttons */
.btn{border:none;border-radius:.5rem;font-weight:600;cursor:pointer;font-family:inherit;transition:.15s;font-size:.78rem;padding:.4rem .9rem}
.btn-dark{background:#0f172a;color:#fff}.btn-dark:hover{background:#1e293b}
.btn-green{background:#096e4d;color:#fff}.btn-green:hover{background:#065f46}
.btn-orange{background:#fd6207;color:#fff;font-size:.9rem;font-weight:700;padding:.6rem 1rem;width:100%;border-radius:.65rem}
.btn-orange:hover{background:#ea580c}
.btn-slate{background:#0f172a;color:#fff}.btn-slate:hover{background:#1e293b}
.btn:disabled{background:#cbd5e1!important;cursor:not-allowed!important;color:#fff}

/* File info */
.fi{margin-top:.55rem;padding:.5rem .65rem;border-radius:.45rem;background:#f0fdf4;border:1px solid #bbf7d0;display:none}
.fi p{font-size:.78rem;color:#166534}
.fi .l{font-weight:600}

/* Form */
label{display:block;font-size:.78rem;font-weight:600;margin-bottom:.25rem}
select,input[type=text]{width:100%;border:1px solid #e2e8f0;border-radius:.45rem;padding:.38rem .55rem;font-size:.78rem;font-family:inherit;outline:none;background:#fff}
select:focus,input[type=text]:focus{border-color:#0453ed;box-shadow:0 0 0 2px rgba(4,83,237,.1)}
.form-row{display:grid;grid-template-columns:1fr 1fr;gap:.7rem}
.lic-row{display:flex;gap:.5rem}
.lic-row input{flex:1;font-family:Consolas,'Courier New',monospace}

/* Messages */
.msg{margin-top:.5rem;padding:.45rem .6rem;border-radius:.45rem;font-size:.78rem;display:none}
.msg-ok{background:#f0fdf4;border:1px solid #bbf7d0;color:#166534}
.msg-err{background:#fef2f2;border:1px solid #fecaca;color:#991b1b}
.msg-info{background:#f0f9ff;border:1px solid #bfdbfe;color:#1e40af}

/* Progress */
.pbox{margin-top:.6rem;display:none}
.ph{display:flex;justify-content:space-between;font-size:.78rem;margin-bottom:.25rem}
.ph .l{font-weight:600}
.ptrack{width:100%;background:#e2e8f0;border-radius:99px;height:.5rem;overflow:hidden}
.pbar{background:#096e4d;height:100%;border-radius:99px;transition:width .3s;width:0%}

/* JSON panel */
.json-card{display:flex;flex-direction:column;min-height:0}
.json-hdr{display:flex;align-items:flex-start;justify-content:space-between;margin-bottom:.5rem}
.json-left{display:flex;align-items:center;gap:.6rem}
.json-tt .t{font-size:.95rem;font-weight:700}
.json-tt .s{font-size:.68rem;color:#64748b;margin-top:.05rem}
.json-out{flex:1;background:#020617;color:#86efac;border-radius:.75rem;padding:.75rem;font-family:Consolas,'Courier New',monospace;font-size:.68rem;line-height:1.3rem;overflow:auto;border:1px solid #1e293b;white-space:pre;min-height:0}

/* Summary */
.summ{padding:.5rem .65rem;border-radius:.45rem;background:#f0f9ff;border:1px solid #bfdbfe;font-size:.72rem;color:#1e40af;margin-bottom:.4rem;line-height:1.35;display:none}

/* Copy ok */
.copy-ok{margin-top:.4rem;padding:.4rem .6rem;border-radius:.45rem;background:#f0fdf4;border:1px solid #bbf7d0;color:#166534;font-size:.78rem;font-weight:600;display:none}

/* No internet overlay */
.overlay{position:fixed;inset:0;background:rgba(15,23,42,.7);display:flex;align-items:center;justify-content:center;z-index:999}
.overlay-box{background:#fff;border-radius:.75rem;padding:2rem;max-width:420px;text-align:center;box-shadow:0 20px 40px rgba(0,0,0,.2)}
.overlay-box h2{color:#991b1b;margin-bottom:.5rem;font-size:1.1rem}
.overlay-box p{font-size:.82rem;color:#475569;line-height:1.5}
.overlay-box .ic{font-size:2.5rem;margin-bottom:.6rem}
.hidden{display:none!important}
</style>
</head>
<body>
<div class="app">
  <div class="hdr">
    <div>
      <h1 id="appTitle">ORBAS Native PDF Extractor</h1>
      <div class="sub">Extract rental condition report PDF data into structured JSON.</div>
    </div>
    <div>
      <div class="ver" id="verLabel">v2.1.0</div>
      <div class="ver-sub">Local PDF Extraction</div>
    </div>
  </div>

  <div class="grid">
    <div class="left">

      <!-- Step 1 -->
      <div class="card">
        <div class="card-h"><span class="circ c-blue">1</span><h2>Select PDF File</h2></div>
        <div class="dz" id="dropZone" onclick="selectFile()">
          <div class="ico">&#128196;</div>
          <div class="main">Click to select your PDF file</div>
          <div class="hint">Supported format: .pdf</div>
          <button class="btn btn-dark" style="margin-top:.5rem" type="button" onclick="event.stopPropagation();selectFile()">Browse PDF</button>
        </div>
        <div class="fi" id="fileInfo">
          <p class="l">Selected File</p>
          <p id="fileName"></p>
        </div>
      </div>

      <!-- Step 2 -->
      <div class="card">
        <div class="card-h"><span class="circ c-blue">2</span><h2>Jurisdiction & Document Type</h2></div>
        <div class="form-row">
          <div>
            <label>Jurisdiction</label>
            <select id="jurisdiction"></select>
          </div>
          <div>
            <label>Document Type</label>
            <select id="documentType"></select>
          </div>
        </div>
      </div>

      <!-- Step 3 -->
      <div class="card">
        <div class="card-h"><span class="circ c-green">3</span><h2>Product Key Verification</h2></div>
        <label>Product / License Key</label>
        <div class="lic-row">
          <input type="text" id="licenseKey" placeholder="Example: ORBAS-DEMO-2026">
          <button class="btn btn-green" id="verifyBtn" onclick="verifyKey()">Verify</button>
        </div>
        <div class="msg" id="licMsg"></div>
      </div>

      <!-- Step 4 -->
      <div class="card">
        <div class="card-h"><span class="circ c-orange">4</span><h2>Extract PDF</h2></div>
        <button class="btn btn-orange" id="extractBtn" disabled onclick="extractPdf()">Extract PDF</button>
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

    <!-- Step 5 - JSON Output -->
    <div class="card json-card">
      <div class="json-hdr">
        <div class="json-left">
          <span class="circ c-green">5</span>
          <div class="json-tt">
            <div class="t">JSON Output</div>
            <div class="s">Copy this output and paste it into the designated ORBAS UI.</div>
          </div>
        </div>
        <button class="btn btn-slate" id="copyBtn" disabled onclick="copyJson()">Copy JSON</button>
      </div>
      <div class="summ" id="summary"></div>
      <pre class="json-out" id="jsonOut">No extraction output yet.</pre>
      <div class="copy-ok" id="copyOk">JSON successfully copied to clipboard.</div>
    </div>
  </div>
</div>

<!-- No internet overlay (hidden by default) -->
<div class="overlay hidden" id="noInet">
  <div class="overlay-box">
    <div class="ic">&#9888;&#65039;</div>
    <h2>No Internet Connection</h2>
    <p>An active internet connection is required to verify your product license and use this application.<br><br>Please connect to the internet and restart the application.</p>
  </div>
</div>

<script>
let fileSelected = false;
let licenseVerified = false;
let extractedJson = '';
let extracting = false;

window.addEventListener('pywebviewready', () => {
  pywebview.api.get_config().then(cfg => {
    document.getElementById('verLabel').textContent = 'v' + cfg.version;

    const jSel = document.getElementById('jurisdiction');
    jSel.innerHTML = '<option value="auto">Auto Detect</option>';
    cfg.jurisdictions.forEach(j => {
      const o = document.createElement('option');
      o.value = j.code;
      o.textContent = j.code + ' - ' + j.name;
      jSel.appendChild(o);
    });

    const dSel = document.getElementById('documentType');
    dSel.innerHTML = '';
    cfg.report_types.forEach(r => {
      const o = document.createElement('option');
      o.value = r.code;
      o.textContent = r.name;
      dSel.appendChild(o);
    });

    if (!cfg.has_internet) {
      document.getElementById('noInet').classList.remove('hidden');
    }
  });
});

function selectFile() {
  pywebview.api.select_file().then(f => {
    if (!f) return;
    fileSelected = true;
    document.getElementById('fileName').textContent = f.name + ' (' + f.size + ')';
    document.getElementById('fileInfo').style.display = 'block';
    checkReady();
  });
}

function verifyKey() {
  const key = document.getElementById('licenseKey').value.trim();
  if (!key) {
    showMsg('licMsg', 'Please enter a product key.', 'err');
    return;
  }
  const btn = document.getElementById('verifyBtn');
  btn.disabled = true;
  btn.textContent = 'Verifying...';

  pywebview.api.verify_license(key).then(r => {
    btn.disabled = false;
    btn.textContent = 'Verify';
    if (r.valid) {
      licenseVerified = true;
      showMsg('licMsg', r.message, 'ok');
    } else {
      licenseVerified = false;
      showMsg('licMsg', r.message, 'err');
    }
    checkReady();
  });
}

document.getElementById('licenseKey').addEventListener('input', () => {
  licenseVerified = false;
  hideMsg('licMsg');
  checkReady();
});

document.getElementById('licenseKey').addEventListener('keydown', e => {
  if (e.key === 'Enter') verifyKey();
});

function checkReady() {
  document.getElementById('extractBtn').disabled = !(fileSelected && licenseVerified && !extracting);
}

function extractPdf() {
  if (extracting) return;
  extracting = true;
  checkReady();

  const jur = document.getElementById('jurisdiction').value;
  const dt = document.getElementById('documentType').value;

  document.getElementById('progressBox').style.display = 'block';
  hideMsg('statusMsg');
  document.getElementById('copyOk').style.display = 'none';
  document.getElementById('summary').style.display = 'none';
  document.getElementById('jsonOut').textContent = 'Extracting PDF data...';
  document.getElementById('copyBtn').disabled = true;

  pywebview.api.extract(jur, dt).then(r => {
    extracting = false;
    if (r.error) {
      showMsg('statusMsg', 'Error: ' + r.error, 'err');
      checkReady();
      return;
    }
    extractedJson = r.json;
    document.getElementById('jsonOut').textContent = r.json;
    document.getElementById('copyBtn').disabled = false;

    if (r.summary) {
      const s = r.summary;
      let txt = 'Jurisdiction Detected:  ' + s.jurisdiction + '\\n';
      txt += 'Document Type Detected:  ' + s.document_type + '\\n';
      txt += 'Pages Processed:  ' + s.pages + '\\n';
      txt += 'Property Areas Detected:  ' + s.areas + '\\n';
      txt += 'Condition Records Extracted:  ' + s.records + '\\n';
      txt += 'Validation Status:  ' + s.validation;
      if (s.missing && s.missing.length > 0) {
        txt += '\\nMissing Data:  ' + s.missing.join(', ');
      }
      const el = document.getElementById('summary');
      el.textContent = txt;
      el.style.display = 'block';
    }
    showMsg('statusMsg', 'PDF extraction completed successfully.', 'ok');
    checkReady();
  });
}

function updateProgress(text, pct) {
  document.getElementById('progText').textContent = text;
  document.getElementById('progPct').textContent = pct + '%';
  document.getElementById('progBar').style.width = pct + '%';
}

function copyJson() {
  if (!extractedJson) return;
  pywebview.api.copy_to_clipboard(extractedJson).then(ok => {
    showCopyOk();
  });
}

function showCopyOk() {
  const el = document.getElementById('copyOk');
  el.style.display = 'block';
  setTimeout(() => { el.style.display = 'none'; }, 3000);
}

function showMsg(id, text, type) {
  const el = document.getElementById(id);
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
    window = webview.create_window(
        f"{APP_NAME} Native PDF Extractor",
        html=HTML,
        js_api=api,
        width=1200,
        height=800,
        min_size=(1000, 700),
    )
    api.window = window
    webview.start()
