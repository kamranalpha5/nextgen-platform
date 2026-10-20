# KEYS-GUIDE — every optional free key for NextGen

**Nothing here is required to run the app.** Every feature except real email
sending works with zero keys. Add a key only when you want that specific
upgrade.

---

## (a) SMTP — real sign-in emails

Without this, the app runs in demo mode: the sign-in code is shown on screen.
To send real emails, use a **Gmail App Password** (free):

1. Go to [myaccount.google.com](https://myaccount.google.com) → **Security**
2. Turn on **2-Step Verification** (required for app passwords)
3. Back in **Security**, open **App passwords**
   (if missing: [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords))
4. App name: `NextGen` → **Generate** → copy the 16-character password
   (spaces can be kept or removed — both work)

Set these env vars (Render dashboard → Environment, or `.env` locally):

| Env var | Value |
|---------|-------|
| `SMTP_HOST` | `smtp.gmail.com` |
| `SMTP_PORT` | `587` |
| `SMTP_USER` | your Gmail address (e.g. `you@gmail.com`) |
| `SMTP_PASS` | the 16-character app password |
| `SMTP_FROM` | your Gmail address (or a friendly name, e.g. `NextGen <you@gmail.com>`) |

Notes:
- Gmail free quota: ~500 emails/day — plenty for sign-in codes.
- Resend's free tier exists ([resend.com](https://resend.com)) but is
  **API-based**, not SMTP — our backend sends via SMTP only, so Gmail App
  Password is the recommended free route.

---

## (b) Hugging Face token — better scripts

Free, upgrades script generation from the built-in template engine to a
hosted LLM:

1. Go to [huggingface.co](https://huggingface.co) → **Sign up** (free)
2. Click your avatar → **Settings** → **Access Tokens**
3. **Create new token** → type **Fine-grained**, permissions **Read**
   (a Read token is all that's needed)
4. Copy the token (starts with `hf_...`)

Set:

| Env var | Value |
|---------|-------|
| `HF_API_TOKEN` | the `hf_...` token |

Without it: the template engine is used automatically — still fully
functional, just less varied than LLM output.

---

## (c) Gemini API key — AI thumbnails from a script

Free from Google AI Studio: open https://aistudio.google.com/apikey, sign in
with any Google account, click "Create API key" (no card needed).

Set:

| Env var | Value |
|---------|-------|
| `GEMINI_API_KEY` | the `AIza...` key |

With it: Thumbnail Studio's "AI thumbnail from your script" panel designs a
thumbnail concept from the pasted script and renders an HD 16:9 image.
Without it: that panel shows a "not configured" note and the manual
editor keeps working as before.

---

## (c) YouTube Data API key — optional reliability upgrade (RESERVED)

The current version fetches channel data with **yt-dlp**, which needs **zero
keys**. A YouTube Data API key is **not used by the app today** — it's
reserved for a possible future upgrade if yt-dlp rate-limiting becomes an
issue.

If you want to have one ready anyway:

1. Go to [console.cloud.google.com](https://console.cloud.google.com)
2. Create a **new project** (any name, e.g. `nextgen-platform`)
3. **APIs & Services → Library** → search **YouTube Data API v3** → **Enable**
4. **APIs & Services → Credentials** → **Create Credentials** → **API key**
5. Copy the key; optionally restrict it to YouTube Data API v3

Set:

| Env var | Value |
|---------|-------|
| `YOUTUBE_API_KEY` | the API key (unused by the current version) |

---

## (d) Database — keep accounts + library across restarts (free Postgres)

Render's free plan wipes the local SQLite file on every restart/redeploy
(accounts and the user library reset to zero). Setting `DATABASE_URL` to a
**free hosted Postgres** fixes this — the app detects it automatically and
uses Postgres instead of SQLite. ~2-minute setup, no code changes:

**Option 1 — Neon (recommended):**
1. Go to [neon.tech](https://neon.tech) → **Sign up** (free, email or GitHub)
2. **Create a new project** (any name, e.g. `nextgen`; pick the region
   nearest to you)
3. Copy the **Connection string** from the project dashboard
   (starts with `postgresql://...` and usually ends with `?sslmode=require`)
4. In Render → your service → **Environment** → add
   `DATABASE_URL` = that connection string → **Save** (service restarts)

**Option 2 — Supabase:**
1. Go to [supabase.com](https://supabase.com) → **Sign up** (free)
2. Create a **new project**
3. **Project Settings → Database** → copy the **Connection string**
   (choose "URI" / pooled if shown)
4. Same step 4 as above in Render.

Notes:
- Neon free tier: 0.5 GB storage, 1 project — plenty for sign-in codes and
  the user library. Supabase free tier is equivalent.
- Without `DATABASE_URL`: the app keeps using local SQLite — fine for
  demo/testing, but accounts reset on redeploy.

---

## Summary

| Feature | Works with zero keys? | Key needed for what |
|---------|----------------------|---------------------|
| Channel analyzer (yt-dlp) | ✅ Yes | — |
| Voiceover MP3 (edge-tts) | ✅ Yes | — |
| Thumbnail maker (canvas) | ✅ Yes | — |
| Niche ideas | ✅ Yes | — |
| Resources directory | ✅ Yes | — |
| Template-based scripts | ✅ Yes | — |
| Demo auth (code on screen) | ✅ Yes | — |
| Real email sending | ❌ No | `SMTP_HOST/PORT/USER/PASS/FROM` (Gmail App Password) |
| LLM-powered scripts | ❌ No (falls back to templates) | `HF_API_TOKEN` (Hugging Face free token) |
| YouTube Data API | ❌ No — **reserved, unused** | `YOUTUBE_API_KEY` (only for a future upgrade) |
| Database persistence | ⚠️ Demo/testing only (resets on redeploy) | `DATABASE_URL` (free Neon/Supabase Postgres) |
