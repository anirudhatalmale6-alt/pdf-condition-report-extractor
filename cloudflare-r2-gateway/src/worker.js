/**
 * ORBAS gated download gateway (Cloudflare Worker + R2)
 * ------------------------------------------------------
 * Serves the ORBAS installer from a PRIVATE R2 bucket, but only after the
 * caller proves they hold a valid, active licence. The raw R2 object is never
 * public - the only way to obtain the file is:
 *
 *   1. POST /api/authorize  { license_key, email }
 *        -> the Worker asks the ORBAS licence server to validate the key.
 *        -> if valid + active it returns a SHORT-LIVED, HMAC-signed download URL.
 *   2. GET  /download/<file>?exp=..&sig=..
 *        -> the Worker verifies the signature + expiry and streams the object
 *           from R2 (Range / resume supported). No licence key in this URL.
 *
 * Also exposed:
 *   GET /            -> a small self-contained download portal (key + email form)
 *   GET /api/version -> current version metadata (from manifest.json in R2)
 *
 * Bindings / config (see wrangler.toml):
 *   BUCKET               R2 bucket binding (private)
 *   LICENSE_VALIDATE_URL ORBAS licence-validation endpoint
 *   DOWNLOAD_SIGNING_KEY secret used to sign download URLs (wrangler secret put)
 *   ALLOWED_ORIGIN       CORS origin for the portal ("*" to allow any)
 *   LINK_TTL_SECONDS     how long a signed download URL stays valid (default 300)
 */

const DEFAULT_TTL = 300; // 5 minutes
const MANIFEST_KEY = "manifest.json";

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    const path = url.pathname;

    // CORS pre-flight for the portal / app.
    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: corsHeaders(env) });
    }

    try {
      if (path === "/" || path === "/index.html") {
        return html(portalPage(), env);
      }
      if (path === "/api/version") {
        return await handleVersion(env);
      }
      if (path === "/api/authorize" && request.method === "POST") {
        return await handleAuthorize(request, env, url);
      }
      if (path === "/api/sync" && request.method === "POST") {
        return await handleSync(request, env);
      }
      if (path === "/api/license-status" && request.method === "GET") {
        return await handleLicenseStatus(request, env, url);
      }
      if (path.startsWith("/download/")) {
        return await handleDownload(request, env, url, path);
      }
      return json({ error: "Not found" }, 404, env);
    } catch (err) {
      return json({ error: "Server error", detail: String(err).slice(0, 200) }, 500, env);
    }
  },
};

/* -------------------------------------------------------------------------- */
/* 1. Authorize: validate licence, hand back a signed short-lived link         */
/* -------------------------------------------------------------------------- */

async function handleAuthorize(request, env, url) {
  let body;
  try {
    body = await request.json();
  } catch {
    return json({ error: "Invalid JSON body" }, 400, env);
  }

  const licenseKey = (body.license_key || body.licenseKey || "").trim();
  const email = (body.email || "").trim();
  if (!licenseKey) {
    return json({ ok: false, error: "License key is required." }, 400, env);
  }

  // Route 1 (synced store): the licence status lives in a Cloudflare KV store
  // that the ORBAS licence server keeps up to date via /api/sync. We read that
  // directly - instant, and no cross-network call to SiteGround at download time.
  // Only if a key is NOT in the store AND LICENSE_SOURCE is explicitly "live"
  // do we fall back to calling the licence server.
  let verdict;
  const rec = await lookupLicense(env, licenseKey);
  if (rec) {
    verdict = storeVerdict(rec, email);
  } else if (env.LICENSE_SOURCE === "live" && env.LICENSE_VALIDATE_URL) {
    verdict = await validateLicense(env, licenseKey, email);
  } else {
    verdict = {
      valid: false,
      error: "Licence not recognised. If you just purchased or renewed, please try again in a few minutes.",
    };
  }
  if (!verdict.valid) {
    // 403 so the client can distinguish "not entitled" from "bad request".
    return json({ ok: false, error: verdict.error || "License is not valid or not active." }, 403, env);
  }

  // Which file to serve: caller may ask for a specific one, else the manifest's.
  const manifest = await readManifest(env);
  const file = sanitizeFile(body.file || (manifest && manifest.file) || "ORBAS-Windows.zip");
  if (!file) {
    return json({ ok: false, error: "No download is available yet." }, 404, env);
  }

  const ttl = Number(env.LINK_TTL_SECONDS) || DEFAULT_TTL;
  const exp = nowSeconds() + ttl;
  const sig = await sign(env, `${file}|${exp}`);
  const downloadPath = `/download/${encodeURIComponent(file)}?exp=${exp}&sig=${sig}`;
  const downloadUrl = new URL(downloadPath, url.origin).toString();

  return json(
    {
      ok: true,
      download_url: downloadUrl,
      expires_in: ttl,
      file,
      version: manifest ? manifest.version : null,
      size: manifest ? manifest.size : null,
      sha256: manifest ? manifest.sha256 : null,
      license_type: verdict.license_type || null,
      max_devices: verdict.max_devices ?? null,
      activated_devices: verdict.activated_devices ?? null,
    },
    200,
    env
  );
}

/* -------------------------------------------------------------------------- */
/* 1b. Sync: the licence server pushes key status into the KV store (webhook)    */
/* -------------------------------------------------------------------------- */

/**
 * POST /api/sync
 *   Headers: X-ORBAS-Sync-Secret: <SYNC_SECRET>
 *   Body (single):  { license_key, email?, status, license_type?,
 *                     subscription_status?, max_devices?, activated_devices? }
 *   Body (bulk):    { records: [ {..}, {..} ] }        // one-time seed / batch
 *   Body (remove):  { license_key, delete: true }      // or status:"deleted"
 * Upserts each record into KV keyed by the (normalised) licence key.
 */
async function handleSync(request, env) {
  const secret = env.SYNC_SECRET || "";
  if (!secret) return json({ ok: false, error: "Sync is not configured on the server." }, 503, env);
  const provided = request.headers.get("X-ORBAS-Sync-Secret") || "";
  if (!provided || !timingSafeEqual(provided, secret)) {
    return json({ ok: false, error: "Unauthorized." }, 401, env);
  }
  if (!env.LICENSES) return json({ ok: false, error: "Licence store is not bound." }, 503, env);

  let body;
  try {
    body = await request.json();
  } catch {
    return json({ ok: false, error: "Invalid JSON body" }, 400, env);
  }

  const records = Array.isArray(body.records) ? body.records : body && (body.license_key || body.licenseKey) ? [body] : null;
  if (!records || records.length === 0) {
    return json({ ok: false, error: "Provide a license_key, or a records array." }, 400, env);
  }
  if (records.length > 1000) {
    return json({ ok: false, error: "Too many records in one request (max 1000). Send in batches." }, 400, env);
  }

  let upserted = 0,
    removed = 0,
    skipped = 0;
  for (const rec of records) {
    const key = normalizeKey(rec.license_key || rec.licenseKey);
    if (!key) {
      skipped++;
      continue;
    }
    const isDelete = rec.delete === true || String(rec.status || "").toLowerCase() === "deleted";
    if (isDelete) {
      await env.LICENSES.delete(kvKey(key));
      removed++;
      continue;
    }
    await env.LICENSES.put(kvKey(key), JSON.stringify(normalizeRecord(rec)));
    upserted++;
  }
  return json({ ok: true, upserted, removed, skipped }, 200, env);
}

/**
 * GET /api/license-status?key=ORBAS-...   (Header: X-ORBAS-Sync-Secret)
 * Lets the licence server confirm a key made it into the store.
 */
async function handleLicenseStatus(request, env, url) {
  const secret = env.SYNC_SECRET || "";
  if (!secret) return json({ ok: false, error: "Sync is not configured on the server." }, 503, env);
  const provided = request.headers.get("X-ORBAS-Sync-Secret") || "";
  if (!provided || !timingSafeEqual(provided, secret)) {
    return json({ ok: false, error: "Unauthorized." }, 401, env);
  }
  const key = normalizeKey(url.searchParams.get("key"));
  if (!key) return json({ ok: false, error: "Provide ?key=" }, 400, env);
  const rec = await lookupLicense(env, key);
  if (!rec) return json({ ok: true, found: false, key }, 200, env);
  return json({ ok: true, found: true, key, record: rec, active: storeVerdict(rec, "").valid }, 200, env);
}

async function lookupLicense(env, licenseKey) {
  if (!env.LICENSES) return null;
  // LICENSES is a dedicated PRIVATE R2 bucket (separate from the download bucket,
  // so licence records can never be reached through the /download path).
  const obj = await env.LICENSES.get(kvKey(normalizeKey(licenseKey)));
  if (!obj) return null;
  try {
    return await obj.json();
  } catch {
    return null;
  }
}

/** Decide entitlement from a stored record (mirrors the live-validation rules). */
function storeVerdict(rec, email) {
  const status = String(rec.status || "").toLowerCase();
  if (["inactive", "expired", "suspended", "revoked", "cancelled", "canceled", "deleted", "disabled"].includes(status)) {
    return { valid: false, error: "This licence is not active. Please renew to download." };
  }
  // Optional email match - only enforced when the stored record carries an email.
  if (rec.email && email && String(rec.email).toLowerCase() !== String(email).toLowerCase()) {
    return { valid: false, error: "This email does not match the licence." };
  }
  const lt = String(rec.license_type || "").toLowerCase();
  if (["subscription", "sub", "paid", "premium"].includes(lt)) {
    if (normalizeStatus(rec.subscription_status) === "inactive") {
      return { valid: false, error: "Your subscription is not active. Please renew to download." };
    }
  }
  return {
    valid: true,
    license_type: rec.license_type || "",
    max_devices: rec.max_devices ?? null,
    activated_devices: rec.activated_devices ?? null,
  };
}

function normalizeRecord(rec) {
  const out = {
    status: String(rec.status || "active").toLowerCase(),
    email: rec.email ? String(rec.email).trim().toLowerCase() : null,
    license_type: (rec.license_type || rec.licence_type || rec.type || "") ? String(rec.license_type || rec.licence_type || rec.type).toLowerCase() : null,
    subscription_status: (rec.subscription_status || rec.subscription || rec.plan_status || "")
      ? String(rec.subscription_status || rec.subscription || rec.plan_status).toLowerCase()
      : null,
    max_devices: rec.max_devices ?? rec.maximum_devices ?? null,
    activated_devices: rec.activated_devices ?? rec.active_devices ?? rec.device_count ?? null,
    updated_at: new Date().toISOString(),
  };
  return out;
}

function normalizeKey(k) {
  if (!k) return null;
  const s = String(k).trim().toUpperCase();
  return s || null;
}
function kvKey(k) {
  return "lic/" + k + ".json";
}

/* -------------------------------------------------------------------------- */
/* 2. Download: verify signature + expiry, stream from R2 (Range supported)     */
/* -------------------------------------------------------------------------- */

async function handleDownload(request, env, url, path) {
  const file = sanitizeFile(decodeURIComponent(path.slice("/download/".length)));
  const exp = Number(url.searchParams.get("exp"));
  const sig = url.searchParams.get("sig") || "";

  if (!file) return json({ error: "Bad file" }, 400, env);
  if (!exp || !sig) return json({ error: "Missing or malformed download link." }, 400, env);
  if (nowSeconds() > exp) {
    return json({ error: "This download link has expired. Please request a new one." }, 410, env);
  }
  const expected = await sign(env, `${file}|${exp}`);
  if (!timingSafeEqual(expected, sig)) {
    return json({ error: "Invalid download link." }, 403, env);
  }

  // Honour HTTP Range so downloads are resumable / restartable.
  const range = parseRange(request.headers.get("Range"));
  const obj = await env.BUCKET.get(file, range ? { range } : undefined);
  if (!obj) return json({ error: "File not found in storage." }, 404, env);

  const headers = new Headers();
  obj.writeHttpMetadata(headers);
  headers.set("etag", obj.httpEtag);
  headers.set("Content-Disposition", `attachment; filename="${file}"`);
  headers.set("Cache-Control", "no-store");
  headers.set("Accept-Ranges", "bytes");

  if (range && obj.range) {
    const total = obj.size;
    const start = obj.range.offset || 0;
    const end = start + (obj.range.length || total - start) - 1;
    headers.set("Content-Range", `bytes ${start}-${end}/${total}`);
    headers.set("Content-Length", String(obj.range.length));
    return new Response(obj.body, { status: 206, headers });
  }

  headers.set("Content-Length", String(obj.size));
  return new Response(obj.body, { status: 200, headers });
}

/* -------------------------------------------------------------------------- */
/* 3. Version metadata (public - no licence needed to CHECK for an update)      */
/* -------------------------------------------------------------------------- */

async function handleVersion(env) {
  const manifest = await readManifest(env);
  if (!manifest) return json({ error: "No manifest published yet." }, 404, env);
  // Only expose what a client needs to decide whether to update. The file is
  // still gated - knowing a version exists does not let you download it.
  return json(
    {
      version: manifest.version,
      file: manifest.file,
      size: manifest.size,
      sha256: manifest.sha256,
      release_notes: manifest.release_notes || "",
      released_at: manifest.released_at || "",
    },
    200,
    env
  );
}

async function readManifest(env) {
  try {
    const obj = await env.BUCKET.get(MANIFEST_KEY);
    if (!obj) return null;
    return await obj.json();
  } catch {
    return null;
  }
}

/* -------------------------------------------------------------------------- */
/* Licence validation - mirrors the desktop app's response handling            */
/* -------------------------------------------------------------------------- */

async function validateLicense(env, licenseKey, email) {
  const endpoint = env.LICENSE_VALIDATE_URL;
  if (!endpoint) return { valid: false, error: "License endpoint not configured." };

  let resp;
  try {
    resp = await fetch(endpoint, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Accept: "application/json",
        "User-Agent": "ORBAS-Download-Gateway/1.0",
      },
      // purpose:"download" lets the licence server treat this as an entitlement
      // check and NOT consume a device slot. Harmless if the server ignores it.
      body: JSON.stringify({ license_key: licenseKey, email, purpose: "download" }),
    });
  } catch {
    return { valid: false, error: "Could not reach the license server. Please try again." };
  }

  if (resp.status === 401 || resp.status === 403) {
    return { valid: false, error: "License key was not accepted." };
  }
  if (resp.status !== 200) {
    return { valid: false, error: `License server returned status ${resp.status}.` };
  }

  let data;
  try {
    data = await resp.json();
  } catch {
    return { valid: false, error: "Invalid response from license server." };
  }

  // Accept valid / active / success, and flatten a nested "license" object -
  // same shape the desktop app tolerates.
  const lic = data && typeof data.license === "object" && data.license ? data.license : {};
  const f = { ...lic, ...stripKey(data, "license") };
  const valid = Boolean(pick(f, ["valid", "active", "success"]) ?? false);
  if (!valid) {
    return { valid: false, error: data.message || data.error || "License is not valid." };
  }

  const status = String(pick(f, ["status"]) || "").toLowerCase();
  if (status && ["inactive", "expired", "suspended", "revoked", "cancelled", "canceled"].includes(status)) {
    return { valid: false, error: data.message || "This license is not active." };
  }

  // Subscription licences must have an active subscription.
  const licenseType = String(pick(f, ["license_type", "licence_type", "type"]) || "").toLowerCase();
  if (["subscription", "sub", "paid", "premium"].includes(licenseType)) {
    const sub = normalizeStatus(pick(f, ["subscription_status", "subscription", "plan_status"]));
    if (sub === "inactive") {
      return { valid: false, error: data.message || "Your subscription is not active. Please renew to download." };
    }
  }

  return {
    valid: true,
    license_type: pick(f, ["license_type", "licence_type", "type"]) || "",
    max_devices: pick(f, ["max_devices", "maximum_devices"]),
    activated_devices: pick(f, ["activated_devices", "active_devices", "device_count"]),
  };
}

function normalizeStatus(v) {
  if (typeof v !== "string") return null;
  const s = v.toLowerCase().replace(/[^a-z]/g, "");
  if (["active", "current", "valid", "trialing"].includes(s)) return "active";
  if (["inactive", "expired", "cancelled", "canceled", "pastdue", "suspended", "none", "lapsed", "unpaid"].includes(s))
    return "inactive";
  return null;
}

/* -------------------------------------------------------------------------- */
/* Crypto helpers (HMAC-SHA256 signed links)                                    */
/* -------------------------------------------------------------------------- */

async function sign(env, message) {
  const secret = env.DOWNLOAD_SIGNING_KEY;
  if (!secret) throw new Error("DOWNLOAD_SIGNING_KEY not set");
  const key = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"]
  );
  const buf = await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(message));
  return base64url(new Uint8Array(buf));
}

function timingSafeEqual(a, b) {
  if (a.length !== b.length) return false;
  let out = 0;
  for (let i = 0; i < a.length; i++) out |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return out === 0;
}

function base64url(bytes) {
  let bin = "";
  for (const b of bytes) bin += String.fromCharCode(b);
  return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

/* -------------------------------------------------------------------------- */
/* Small utilities                                                              */
/* -------------------------------------------------------------------------- */

function nowSeconds() {
  return Math.floor(Date.now() / 1000);
}

// Only allow simple file names inside the bucket - block path traversal.
function sanitizeFile(name) {
  if (!name) return null;
  name = String(name).trim().replace(/^\/+/, "");
  if (name.includes("..") || name.includes("\\") || name.length > 200) return null;
  if (!/^[A-Za-z0-9._\-\/]+$/.test(name)) return null;
  return name;
}

function parseRange(header) {
  if (!header) return null;
  const m = /^bytes=(\d*)-(\d*)$/.exec(header.trim());
  if (!m) return null;
  const start = m[1] === "" ? undefined : Number(m[1]);
  const end = m[2] === "" ? undefined : Number(m[2]);
  if (start === undefined && end === undefined) return null;
  if (start !== undefined && end !== undefined) return { offset: start, length: end - start + 1 };
  if (start !== undefined) return { offset: start };
  return { suffix: end }; // last N bytes
}

function pick(obj, keys) {
  for (const k of keys) if (obj[k] !== undefined && obj[k] !== null && obj[k] !== "") return obj[k];
  return undefined;
}
function stripKey(obj, key) {
  const out = {};
  for (const k in obj) if (k !== key) out[k] = obj[k];
  return out;
}

function corsHeaders(env) {
  return {
    "Access-Control-Allow-Origin": env.ALLOWED_ORIGIN || "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
  };
}
function json(obj, status, env) {
  return new Response(JSON.stringify(obj, null, 2), {
    status: status || 200,
    headers: { "Content-Type": "application/json", ...corsHeaders(env) },
  });
}
function html(markup, env) {
  return new Response(markup, {
    status: 200,
    headers: { "Content-Type": "text/html; charset=utf-8", ...corsHeaders(env) },
  });
}

/* -------------------------------------------------------------------------- */
/* Download portal (self-contained, no external assets)                         */
/* -------------------------------------------------------------------------- */

function portalPage() {
  return `<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ORBAS - Download</title>
<style>
:root{--navy:#1f3a5f;--accent:#2a6f97;--bg:#f3f6fa;--card:#fff;--err:#b23b3b;--ok:#2e7d32}
*{box-sizing:border-box}body{margin:0;font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;background:var(--bg);color:#20303f;display:flex;min-height:100vh;align-items:center;justify-content:center;padding:20px}
.card{background:var(--card);max-width:440px;width:100%;border-radius:14px;box-shadow:0 10px 40px rgba(31,58,95,.12);padding:32px 30px}
h1{margin:0 0 4px;font-size:22px;color:var(--navy)}
.sub{color:#5a6b7b;font-size:13px;margin-bottom:22px}
label{display:block;font-size:13px;font-weight:600;margin:14px 0 6px;color:var(--navy)}
input{width:100%;padding:11px 12px;border:1px solid #cdd8e3;border-radius:8px;font-size:14px}
input:focus{outline:none;border-color:var(--accent);box-shadow:0 0 0 3px rgba(42,111,151,.15)}
button{width:100%;margin-top:22px;padding:12px;border:0;border-radius:8px;background:var(--accent);color:#fff;font-size:15px;font-weight:600;cursor:pointer}
button:disabled{opacity:.6;cursor:default}
.msg{margin-top:16px;font-size:13.5px;padding:11px 12px;border-radius:8px;display:none}
.msg.err{display:block;background:#faeaea;color:var(--err)}
.msg.ok{display:block;background:#e9f4ea;color:var(--ok)}
.foot{margin-top:20px;font-size:11.5px;color:#8194a4;text-align:center}
</style></head><body>
<div class="card">
  <h1>ORBAS Download</h1>
  <div class="sub">Enter your licence details to download the latest version.</div>
  <label for="key">Licence Key</label>
  <input id="key" placeholder="ORBAS-AUS-..." autocomplete="off" spellcheck="false">
  <label for="email">Email</label>
  <input id="email" type="email" placeholder="you@example.com" autocomplete="off">
  <button id="go">Get Download</button>
  <div id="msg" class="msg"></div>
  <div class="foot">Your download link is personal and expires shortly after it is issued.</div>
</div>
<script>
const $=id=>document.getElementById(id);
$('go').addEventListener('click',async()=>{
  const key=$('key').value.trim(),email=$('email').value.trim(),msg=$('msg'),btn=$('go');
  msg.className='msg';msg.textContent='';
  if(!key){msg.className='msg err';msg.textContent='Please enter your licence key.';return;}
  btn.disabled=true;btn.textContent='Checking...';
  try{
    const r=await fetch('/api/authorize',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({license_key:key,email})});
    const d=await r.json();
    if(r.ok&&d.ok&&d.download_url){
      msg.className='msg ok';msg.textContent='Licence verified. Your download is starting'+(d.version?' (v'+d.version+')':'')+'...';
      window.location.href=d.download_url;
    }else{
      msg.className='msg err';msg.textContent=d.error||'Could not verify your licence.';
    }
  }catch(e){msg.className='msg err';msg.textContent='Network error. Please try again.';}
  btn.disabled=false;btn.textContent='Get Download';
});
</script>
</body></html>`;
}
