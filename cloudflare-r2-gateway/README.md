# ORBAS Gated Download Gateway (Cloudflare Worker + R2)

Serves the ORBAS installer from a **private** R2 bucket, but only to callers who
present a **valid, active licence**. The raw R2 object is never public — the file
can only be reached through a short-lived, signed link that is issued *after* the
licence server approves the request.

```
  Browser / App
       │  1. POST /api/authorize { license_key, email }
       ▼
  ┌─────────────────────┐   validate    ┌────────────────────────────┐
  │  Worker (this repo) │ ─────────────▶│ ORBAS licence server        │
  │                     │◀───────────── │ /api/license/validate       │
  └─────────────────────┘   valid?      └────────────────────────────┘
       │  2. returns signed URL:
       │     /download/ORBAS-Windows.zip?exp=..&sig=..   (valid ~5 min)
       ▼
  GET /download/... ──▶ Worker verifies sig+expiry ──▶ streams file from R2
```

Why this design:
- The installer sits in a **private** bucket, so nobody can grab it by guessing a URL.
- The licence key **never appears** in the file URL, so shared links leak nothing.
- Signed links **expire quickly**, so a link can't be reposted and reused for long.
- Range requests are supported, so downloads are **resumable**.

---

## Endpoints

| Method | Path | Auth | Purpose |
|--------|------|------|---------|
| `GET`  | `/` | none | Download portal (licence key + email form) |
| `GET`  | `/api/version` | none | Current version metadata (to check for updates) |
| `POST` | `/api/authorize` | licence | Validate licence → return a signed download URL |
| `GET`  | `/download/<file>?exp&sig` | signature | Stream the file from R2 |

`POST /api/authorize` body:
```json
{ "license_key": "ORBAS-AUS-SUB-XXXX", "email": "you@example.com" }
```
Success:
```json
{
  "ok": true,
  "download_url": "https://downloads.example.com/download/ORBAS-Windows.zip?exp=..&sig=..",
  "expires_in": 300,
  "file": "ORBAS-Windows.zip",
  "version": "3.7.10",
  "size": 58320837,
  "sha256": "..."
}
```
Rejected (bad key / inactive subscription / device policy):
```json
{ "ok": false, "error": "Your subscription is not active. Please renew to download." }
```

---

## One-time setup

1. **Install Wrangler and log in**
   ```bash
   npm install
   npx wrangler login
   ```

2. **Create the private bucket** (leave public access OFF)
   ```bash
   npx wrangler r2 bucket create <YOUR_R2_BUCKET_NAME>
   ```

3. **Fill in `wrangler.toml`**
   - `bucket_name` → your bucket
   - `routes.pattern` → your download domain, e.g. `downloads.orbas.com.au`
   - `LICENSE_VALIDATE_URL` is pre-set to `https://app.orbas.com.au/api/license/validate`

4. **Set the link-signing secret** (any long random string; keep it private)
   ```bash
   npx wrangler secret put DOWNLOAD_SIGNING_KEY
   ```

5. **Upload the installer + manifest to R2**
   ```bash
   npx wrangler r2 object put <BUCKET>/ORBAS-Windows.zip --file ./ORBAS-Windows.zip
   npx wrangler r2 object put <BUCKET>/manifest.json     --file ./manifest.json
   ```
   Copy `manifest.example.json` → `manifest.json` and fill in the version/size/sha256.
   Get the sha256 with: `sha256sum ORBAS-Windows.zip`.

6. **Deploy**
   ```bash
   npx wrangler deploy
   ```

7. **Custom domain** — with `custom_domain = true` in `wrangler.toml`, Cloudflare
   provisions `downloads.<your-domain>` and its TLS certificate automatically on
   deploy, provided the zone is on your Cloudflare account. (Alternatively, add
   the route by hand in the dashboard: *Workers & Pages → your worker → Triggers
   → Custom Domains*.)

---

## Publishing a new version later

```bash
# 1. upload the new build
npx wrangler r2 object put <BUCKET>/ORBAS-Windows.zip --file ./ORBAS-Windows.zip
# 2. update manifest.json (version, size, sha256, notes) and upload it
npx wrangler r2 object put <BUCKET>/manifest.json --file ./manifest.json
```
No redeploy needed — the Worker reads the manifest live. The desktop app can call
`GET /api/version`, compare against its own version, and prompt the user to update.

---

## Notes for the licence server

`/api/authorize` POSTs `{ license_key, email, purpose: "download" }` to
`LICENSE_VALIDATE_URL`. The `purpose: "download"` flag lets the licence server
treat this as an **entitlement check** and *not* consume a device slot (a download
shouldn't burn one of the plan's device activations). If the server ignores the
flag, everything still works — it just means a validate call is logged.

The Worker accepts the same response shapes the desktop app does: a top-level
`valid` / `active` / `success`, and/or a nested `license` object with
`status`, `license_type`, `subscription_status`, `max_devices`,
`activated_devices`. Send back a `message` field on rejection and the portal will
show it to the user verbatim.
