# NextGen — Free Faceless-YouTube Toolkit

A free toolkit for building faceless YouTube channels: channel analyzer, script
generator, AI voiceover (MP3), thumbnail maker, niche ideas and resource links.

**Stack:** Python 3.11 · FastAPI (`app/main.py`) · vanilla-JS SPA (`/static`)
· SQLite (`data/nextgen.db`) · cookie sessions (itsdangerous).

---

## Free deployment on Render (step by step)

### 1. Create a free Render account
Go to [render.com](https://render.com) → **Sign Up** (GitHub sign-up is easiest).

### 2. Push this project to GitHub
```bash
cd ~/workspace/nextgen-platform
git init
git add -A
git commit -m "NextGen platform"
gh repo create nextgen-platform --public --source=. --push
# (or create the repo on github.com manually, then:)
git remote add origin https://github.com/YOUR-USERNAME/nextgen-platform.git
git branch -M main
git push -u origin main
```
Make sure `Dockerfile`, `render.yaml`, `requirements.txt`, `app/`, and `static/` are all committed.

### 3. Deploy with a Blueprint
1. Render dashboard → **New +** → **Blueprint**
2. Connect your GitHub account and select the `nextgen-platform` repo
3. Render reads `render.yaml` and auto-configures everything
4. Review the service name (`nextgen-platform`) and click **Apply**

### 4. Set environment variables
In the service dashboard → **Environment**:
- `SECRET_KEY` — **auto-generated** by the blueprint (do not change it later;
  changing it logs everyone out)
- `PORT`, `DATA_DIR` — already set by the blueprint
- Optional (see [KEYS-GUIDE.md](KEYS-GUIDE.md) for how to get them):
  - `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS`, `SMTP_FROM` — real
    sign-in emails. **Not required:** without these the app runs in demo mode
    and shows the sign-in code on screen.
  - `HF_API_TOKEN` — better script quality via Hugging Face. Optional.

### 5. Deploy and open the URL
Click **Manual Deploy** → **Deploy latest commit**. When the build finishes,
Render gives you a URL like `https://nextgen-platform.onrender.com`.
The health check at `/api/health` confirms the app is up.

### 6. Test it
1. Open the Render URL → the app loads (vanilla-JS SPA).
2. Sign in with any email — demo mode displays the verification code on screen.
3. Open the **Channel Analyzer** → paste a YouTube channel URL → submit →
   watch the job progress → download the JSON report.
4. Try **Voiceover** → enter text → submit → poll the job → download the MP3.

---

## Local run

```bash
cd ~/workspace/nextgen-platform
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then edit SECRET_KEY at minimum
uvicorn app.main:app --reload
```
Open http://127.0.0.1:8000. Without `SMTP_HOST` the app uses demo auth
(sign-in code shown on screen). ffmpeg is required locally for voiceover MP3
output (installed automatically in Docker).

---

## Architecture overview

```
┌──────────────────────────┐
│  Browser (vanilla JS)    │  SPA served from /static
└────────────┬─────────────┘
             │  HTTP (cookie sessions via itsdangerous)
┌────────────▼─────────────┐
│  FastAPI  app/main.py    │
│  ├─ auth (email + code)  │
│  ├─ jobs (background)    │
│  └─ tools endpoints      │
└──┬──────────────┬────────┘
   │              │
SQLite        worker threads
data/nextgen.db  (analyze / script / voiceover)
```

- **Auth:** email + one-time code. `DEMO_AUTH` is `"1"` automatically when
  `SMTP_HOST` is unset → code displayed on screen instead of emailed.
- **Heavy work is job-based** so requests return fast: submit → get `job_id` →
  poll status → download results.
- **Data:** SQLite at `$DATA_DIR/nextgen.db` (default `data/nextgen.db`).

## API endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/health` | Health check (Render uses this) |
| POST | `/api/auth/request-code` | Send sign-in code to email (or show in demo mode) |
| POST | `/api/auth/verify` | Verify code → sets session cookie |
| POST | `/api/jobs/analyze` | Start channel analysis → `202 {job_id}` |
| POST | `/api/jobs/script` | Start script generation → `202 {job_id}` |
| POST | `/api/jobs/voiceover` | Start voiceover render → `202 {job_id}` |
| GET | `/api/jobs/{job_id}` | Poll job status / progress |
| GET | `/api/jobs/{job_id}/download` | Download job result (JSON) |
| GET | `/api/jobs/{job_id}/audio` | Download voiceover result (MP3) |

## Free-tier limits (Render)

- **Sleeps after ~15 min of inactivity** → the first request after sleep takes
  ~30–60 s to wake the service. Keep-alive pings help but don't guarantee it.
- **Ephemeral disk** → the SQLite database and job files **reset on every
  redeploy or restart**. Acceptable for the free tier; attach a persistent
  disk (paid plan) mounted at `/app/data` to keep data.
- **750 hours/month** of free instance time — enough for one service running
  full-time. Long voiceover/script jobs still finish, but generation speed
  is limited by free-plan CPU.
- Cold builds: Docker build on the free plan takes a few minutes per deploy.
