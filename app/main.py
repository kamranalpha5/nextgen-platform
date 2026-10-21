#!/usr/bin/env python3
"""NextGen — free faceless-YouTube toolkit platform. FastAPI backend.

Run:  uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}
All config comes from environment variables (see .env.example).
"""

import asyncio
import concurrent.futures
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import smtplib
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from itsdangerous import BadSignature, URLSafeSerializer

try:
    import psycopg2
    import psycopg2.extras
except ImportError:  # sqlite-only installs: only needed when DATABASE_URL is set
    psycopg2 = None  # type: ignore[assignment]

# ----------------------------------------------------------------------------
# Config (everything from env, with sensible defaults)
# ----------------------------------------------------------------------------
SECRET_KEY = os.getenv("SECRET_KEY")
if not SECRET_KEY:
    SECRET_KEY = "dev-secret-key-change-in-production"
    logging.basicConfig(level=logging.INFO)
    logging.getLogger("nextgen").warning(
        "SECRET_KEY is not set — using an insecure dev default. "
        "Set SECRET_KEY in production!"
    )

SMTP_HOST = os.getenv("SMTP_HOST", "").strip()
SMTP_PORT = int(os.getenv("SMTP_PORT", "587") or 587)
SMTP_USER = os.getenv("SMTP_USER", "").strip()
SMTP_PASS = os.getenv("SMTP_PASS", "")
SMTP_FROM = os.getenv("SMTP_FROM", "").strip() or SMTP_USER
HF_API_TOKEN = os.getenv("HF_API_TOKEN", "").strip()
PORT = int(os.getenv("PORT", "8000") or 8000)
DATA_DIR = Path(os.getenv("DATA_DIR", "./data")).resolve()
# DATABASE_URL: when set, user accounts + library persist on a hosted Postgres
# (free Neon/Supabase) instead of the ephemeral local SQLite file. This is how
# the app survives Render free-plan restarts/redeploys. Unset = SQLite default.
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
USE_POSTGRES = bool(DATABASE_URL)
# DEMO_AUTH defaults to "1" when no SMTP is configured (auto), else "0".
_demo_default = "1" if not SMTP_HOST else "0"
DEMO_AUTH = os.getenv("DEMO_AUTH", _demo_default).strip() == "1"
CORS_ORIGINS = os.getenv("CORS_ORIGINS", "").strip()

DATA_DIR.mkdir(parents=True, exist_ok=True)
(DB_AUDIO_DIR := DATA_DIR / "audio").mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "nextgen.db"

APP_ROOT = Path(__file__).resolve().parent          # .../nextgen-platform/app
PROJECT_ROOT = APP_ROOT.parent                       # .../nextgen-platform
STATIC_DIR = PROJECT_ROOT / "static"
NICHE_FILE = APP_ROOT / "data" / "niches.json"

logger = logging.getLogger("nextgen")
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

# ----------------------------------------------------------------------------
# Database — SQLite (default) or Postgres when DATABASE_URL is set
# ----------------------------------------------------------------------------
# Default: local SQLite file (stdlib). Set DATABASE_URL to a Postgres
# connection string (e.g. free Neon/Supabase) for persistent accounts +
# library on Render's free plan, which wipes local files on restart/redeploy.
# Auth codes + user library only — generated audio files stay on DATA_DIR.
_db_lock = threading.Lock()


class _PGCursor:
    """sqlite3-cursor-shaped wrapper over a psycopg2 RealDictCursor."""

    def __init__(self, cur):
        self._cur = cur

    def fetchone(self):
        row = self._cur.fetchone()
        return dict(row) if row is not None else None

    def fetchall(self):
        return [dict(r) for r in self._cur.fetchall()]

    @property
    def rowcount(self):
        return self._cur.rowcount


class _PGConn:
    """sqlite3-connection-shaped wrapper over a psycopg2 connection.

    Translates the sqlite-style '?' placeholders used at every call site to
    Postgres '%s', so the app code stays identical for both backends.
    """

    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql, params=()):
        cur = self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(sql.replace("?", "%s"), params)
        return _PGCursor(cur)

    def executescript(self, sql):
        with self._conn.cursor() as cur:
            cur.execute(sql)
        self._conn.commit()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                self._conn.commit()
            else:
                self._conn.rollback()
        finally:
            self._conn.close()
        return False


def _db():
    """New DB connection per call (same as the previous SQLite behaviour)."""
    if USE_POSTGRES:
        if psycopg2 is None:
            raise RuntimeError(
                "DATABASE_URL is set but psycopg2 is not installed — "
                "add psycopg2-binary to requirements.txt and redeploy."
            )
        return _PGConn(psycopg2.connect(DATABASE_URL))
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    if USE_POSTGRES:
        schema = """
            CREATE TABLE IF NOT EXISTS auth_codes (
                email      TEXT PRIMARY KEY,
                code_hash  TEXT NOT NULL,
                expires_at BIGINT NOT NULL,
                created_at BIGINT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS library (
                id         SERIAL PRIMARY KEY,
                email      TEXT NOT NULL,
                type       TEXT NOT NULL,
                title      TEXT NOT NULL,
                payload    TEXT NOT NULL,
                created_at BIGINT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_library_email ON library(email);
            """
    else:
        schema = """
            CREATE TABLE IF NOT EXISTS auth_codes (
                email      TEXT PRIMARY KEY,
                code_hash  TEXT NOT NULL,
                expires_at INTEGER NOT NULL,
                created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS library (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                email      TEXT NOT NULL,
                type       TEXT NOT NULL,
                title      TEXT NOT NULL,
                payload    TEXT NOT NULL,
                created_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_library_email ON library(email);
            """
    with _db_lock, _db() as conn:
        conn.executescript(schema)


init_db()
logger.info(
    "Database backend: %s",
    "Postgres (DATABASE_URL set)" if USE_POSTGRES else f"SQLite ({DB_PATH})",
)

# ----------------------------------------------------------------------------
# In-memory sliding-window rate limiter, keyed by (ip, endpoint)
# ----------------------------------------------------------------------------
_rate_buckets: dict[tuple, list] = {}
_rate_lock = threading.Lock()


def rate_limited(key: tuple, limit: int, window_s: int = 3600) -> bool:
    """Return True if the bucket is exhausted (and record nothing)."""
    now = time.time()
    with _rate_lock:
        hits = [t for t in _rate_buckets.get(key, []) if now - t < window_s]
        if len(hits) >= limit:
            _rate_buckets[key] = hits
            return True
        hits.append(now)
        _rate_buckets[key] = hits
        return False


def _rate_limit_response(limit: int) -> JSONResponse:
    return JSONResponse(
        status_code=429,
        content={"ok": False, "error": f"Rate limit exceeded ({limit}/hour). Try again later."},
    )


def get_client_ip(request: Request) -> str:
    """Render/proxy-aware client IP: X-Forwarded-For first."""
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


# ----------------------------------------------------------------------------
# Sessions — itsdangerous signed cookie
# ----------------------------------------------------------------------------
_serializer = URLSafeSerializer(SECRET_KEY, salt="nextgen-session-v1")
SESSION_COOKIE = "nextgen_session"
SESSION_MAX_AGE = 30 * 24 * 3600  # 30 days


def make_session_cookie(email: str) -> str:
    return _serializer.dumps({"email": email, "iat": int(time.time())})


def get_session_email(request: Request) -> str | None:
    raw = request.cookies.get(SESSION_COOKIE)
    if not raw:
        return None
    try:
        data = _serializer.loads(raw, max_age=SESSION_MAX_AGE)
        email = data.get("email")
        return email if isinstance(email, str) and email else None
    except BadSignature:
        return None


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
CODE_TTL = 10 * 60  # 6-digit codes live 10 minutes


def valid_email(email: str) -> bool:
    return bool(email) and len(email) <= 254 and bool(EMAIL_RE.match(email.strip()))


def hash_code(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()


def now_ts() -> int:
    return int(time.time())


def send_code_email(to_email: str, code: str) -> None:
    """Send the sign-in code via SMTP with STARTTLS. Never logs code/pass."""
    msg = EmailMessage()
    msg["Subject"] = "Your NextGen sign-in code"
    msg["From"] = SMTP_FROM
    msg["To"] = to_email
    msg.set_content(
        f"Your NextGen sign-in code is: {code}\n\n"
        "It expires in 10 minutes. If you didn't request this, just ignore this email."
    )
    # NOTE: we never log the code or SMTP_PASS here.
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as smtp:
        smtp.starttls()
        if SMTP_USER:
            smtp.login(SMTP_USER, SMTP_PASS)
        smtp.send_message(msg)


# ----------------------------------------------------------------------------
# FastAPI app
# ----------------------------------------------------------------------------
app = FastAPI(title="NextGen", version="1.0.0")

if CORS_ORIGINS:
    # Permissive CORS only when explicitly configured (e.g. separate frontend domain).
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[o.strip() for o in CORS_ORIGINS.split(",") if o.strip()],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    logger.info("CORS enabled for origins: %s", CORS_ORIGINS)

# Frontend (built by a separate agent) is served from the same origin.
STATIC_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")


@app.get("/", include_in_schema=False)
async def root():
    return RedirectResponse(url="/static/", status_code=307)


# ----------------------------------------------------------------------------
# Auth endpoints
# ----------------------------------------------------------------------------
@app.post("/api/auth/request-code")
async def request_code(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"ok": False, "error": "Invalid JSON body."})
    email = str(body.get("email", "")).strip().lower()
    if not valid_email(email):
        return JSONResponse(status_code=400, content={"ok": False, "error": "Invalid email address."})

    ip = get_client_ip(request)
    if rate_limited((ip, "auth/request-code"), 20):
        return _rate_limit_response(20)
    if rate_limited((email, "auth/request-code"), 5):
        return JSONResponse(
            status_code=429,
            content={"ok": False, "error": "Too many codes requested for this email (5/hour)."},
        )

    code = f"{secrets.randbelow(900000) + 100000:06d}"
    expires_at = now_ts() + CODE_TTL
    with _db_lock, _db() as conn:
        if USE_POSTGRES:
            conn.execute(
                "INSERT INTO auth_codes (email, code_hash, expires_at, created_at)"
                " VALUES (?, ?, ?, ?)"
                " ON CONFLICT (email) DO UPDATE SET"
                " code_hash = EXCLUDED.code_hash,"
                " expires_at = EXCLUDED.expires_at,"
                " created_at = EXCLUDED.created_at",
                (email, hash_code(code), expires_at, now_ts()),
            )
        else:
            conn.execute(
                "INSERT OR REPLACE INTO auth_codes (email, code_hash, expires_at, created_at)"
                " VALUES (?, ?, ?, ?)",
                (email, hash_code(code), expires_at, now_ts()),
            )

    if SMTP_HOST and not DEMO_AUTH:
        try:
            send_code_email(email, code)
        except Exception as exc:  # never leak SMTP details to the client
            logger.error("SMTP send failed for %s: %s", email, type(exc).__name__)
            return JSONResponse(
                status_code=500,
                content={"ok": False, "error": "Could not send the email. Please try again later."},
            )
        logger.info("Sign-in code emailed to %s", email)
        return {"ok": True, "demo": False, "demo_code": None}

    # Demo mode: return the code directly (also logged server-side for dev).
    logger.info("DEMO sign-in code for %s: %s", email, code)
    return {"ok": True, "demo": True, "demo_code": code}


@app.post("/api/auth/verify")
async def verify_code(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"ok": False, "error": "Invalid JSON body."})
    email = str(body.get("email", "")).strip().lower()
    code = str(body.get("code", "")).strip()
    if not valid_email(email) or not re.fullmatch(r"\d{6}", code):
        return JSONResponse(
            status_code=400, content={"ok": False, "error": "Invalid email or code format."}
        )

    if rate_limited((email, "auth/verify"), 10):
        return JSONResponse(
            status_code=429,
            content={"ok": False, "error": "Too many attempts for this email (10/hour)."},
        )

    with _db_lock, _db() as conn:
        row = conn.execute(
            "SELECT code_hash, expires_at FROM auth_codes WHERE email = ?", (email,)
        ).fetchone()

    if row is None or not hmac.compare_digest(row["code_hash"], hash_code(code)):
        return JSONResponse(
            status_code=400, content={"ok": False, "error": "Wrong code. Please try again."}
        )
    if row["expires_at"] < now_ts():
        with _db_lock, _db() as conn:
            conn.execute("DELETE FROM auth_codes WHERE email = ?", (email,))
        return JSONResponse(
            status_code=400, content={"ok": False, "error": "Code expired. Request a new one."}
        )

    # Single-use: burn the code.
    with _db_lock, _db() as conn:
        conn.execute("DELETE FROM auth_codes WHERE email = ?", (email,))

    resp = JSONResponse(status_code=200, content={"ok": True, "email": email})
    resp.set_cookie(
        SESSION_COOKIE,
        make_session_cookie(email),
        httponly=True,
        samesite="lax",
        path="/",
        max_age=SESSION_MAX_AGE,
    )
    logger.info("User signed in: %s", email)
    return resp


@app.post("/api/auth/logout")
async def logout():
    resp = JSONResponse(status_code=200, content={"ok": True})
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp


@app.get("/api/auth/me")
async def me(request: Request):
    email = get_session_email(request)
    if not email:
        return JSONResponse(status_code=401, content={"error": "unauthorized"})
    return {"email": email}

# ----------------------------------------------------------------------------
# Jobs — in-memory job registry + background threads + 1h result cache
# ----------------------------------------------------------------------------
_jobs: dict = {}
_jobs_lock = threading.Lock()
_cache: dict = {}  # (kind, cache_key) -> (expires_ts, result)
_cache_lock = threading.Lock()


def _new_job(kind: str) -> str:
    job_id = secrets.token_urlsafe(16)
    with _jobs_lock:
        # Prune jobs older than 24h so the dict can't grow forever.
        cutoff = time.time() - 24 * 3600
        for jid in [j for j, j in _jobs.items() if j["created"] < cutoff]:
            del _jobs[jid]
        _jobs[job_id] = {
            "kind": kind,
            "status": "pending",
            "result": None,
            "error": None,
            "created": time.time(),
        }
    return job_id


def _set_job(job_id: str, **fields) -> None:
    with _jobs_lock:
        if job_id in _jobs:
            _jobs[job_id].update(fields)


def _get_job(job_id: str) -> dict | None:
    with _jobs_lock:
        job = _jobs.get(job_id)
        return dict(job) if job else None


def cache_get(kind: str, key: str):
    with _cache_lock:
        hit = _cache.get((kind, key))
        if hit and hit[0] > time.time():
            return hit[1]
        if hit:
            del _cache[(kind, key)]
    return None


def cache_put(kind: str, key: str, result, ttl: int = 3600) -> None:
    with _cache_lock:
        _cache[(kind, key)] = (time.time() + ttl, result)


# ----------------------------------------------------------------------------
# POST /api/jobs/analyze — YouTube channel analysis via yt-dlp
# ----------------------------------------------------------------------------
YOUTUBE_URL_RE = re.compile(
    r"^https?://(www\.|m\.)?(youtube\.com|youtu\.be)/"
    r"(@[\w.\-]+|channel/[\w.\-]+|c/[\w.\-]+|user/[\w.\-]+"
    r"|watch\?[^ \s]*v=[\w\-]+|playlist\?[^ \s]*list=[\w\-]+"
    r"|shorts/[\w\-]+|embed/[\w\-]+)",
    re.IGNORECASE,
)


def _collect_video_entries(data: dict, max_videos: int) -> list:
    """Flatten yt-dlp's tab/playlist nesting into a list of video entry dicts.

    A channel URL yields tab sub-playlists (Videos / Live / Shorts) whose
    entries are the real videos; a plain playlist/watch URL yields video
    entries directly (or a single video dict with no 'entries' key).
    """
    raw = [e for e in (data.get("entries") or []) if e]

    # Prefer the "Videos" tab for channel URLs.
    tab = None
    for e in raw:
        nested = [x for x in (e.get("entries") or []) if x]
        if nested and e.get("_type") == "playlist":
            if "videos" in (e.get("title") or "").lower():
                tab = nested
                break
    if tab is None:
        # No Videos tab: merge whatever we find (plain playlists, watch URLs).
        tab = []
        for e in raw:
            nested = [x for x in (e.get("entries") or []) if x]
            if nested and e.get("_type") == "playlist":
                tab.extend(nested)
            else:
                tab.append(e)
    if not tab and data.get("id") and data.get("_type") in (None, "video"):
        tab = [data]  # single video URL, no 'entries' wrapper
    return tab[:max_videos]


def _enrich_views(videos: list, timeout_per: int = 30, workers: int = 5) -> None:
    """Best-effort view-count enrichment.

    --flat-playlist entries don't carry view counts, so fetch each video's
    view_count with a lightweight per-video yt-dlp call, in parallel.
    Datacenter IPs are walled for some player clients, so each video is
    tried on android, then ios, then web; failures are ignored — views
    simply stay None.
    """
    def fetch(video: dict) -> None:
        for client in ("android", "ios", "web"):
            try:
                proc = subprocess.run(
                    [sys.executable, "-m", "yt_dlp",
                     "--no-playlist", "--no-warnings",
                     "--print", "%(view_count)s",
                     "--print", "%(upload_date)s",
                     "--extractor-args", f"youtube:player_client={client}",
                     video["url"]],
                    capture_output=True, text=True, timeout=timeout_per,
                )
                out = [ln.strip() for ln in (proc.stdout or "").strip().splitlines()]
                got = False
                if out and out[0].isdigit():
                    video["views"] = int(out[0])
                    got = True
                if len(out) > 1 and len(out[1]) == 8 and out[1].isdigit() \
                        and not video.get("upload_date"):
                    video["upload_date"] = out[1]
                    video["date"] = f"{out[1][:4]}-{out[1][4:6]}-{out[1][6:8]}"
                    got = True
                if got:
                    return
            except Exception:
                continue

    pending = [v for v in videos if v.get("views") is None or not v.get("upload_date")]
    if not pending:
        return
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(fetch, pending))


def _parse_analyze(data: dict, max_videos: int) -> dict:
    videos = []
    for e in _collect_video_entries(data, max_videos):
        vid = e.get("id")
        if not vid or e.get("_type") == "playlist":
            continue  # skip private/deleted (null) and stray playlists
        upload_date = e.get("upload_date")
        ts = e.get("timestamp")
        if not upload_date and isinstance(ts, (int, float)):
            upload_date = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y%m%d")
        videos.append(
            {
                "id": vid,
                "title": e.get("title") or "Untitled",
                "url": f"https://www.youtube.com/watch?v={vid}",
                "views": e.get("view_count"),
                "duration": e.get("duration"),
                "upload_date": upload_date,
                # Frontend-friendly aliases:
                "date": (
                    f"{upload_date[:4]}-{upload_date[4:6]}-{upload_date[6:8]}"
                    if upload_date and len(upload_date) == 8 and upload_date.isdigit()
                    else upload_date
                ),
            }
        )
    channel = data.get("channel") or data.get("uploader") or ""
    channel_id = data.get("channel_id") or data.get("id") or ""
    followers = data.get("channel_follower_count")

    return {
        "channel": {
            "name": channel,
            "id": channel_id,
            # Aliases the frontend reads:
            "handle": channel_id,
            "subscribers": followers,
            "follower_count": followers,
            "url": data.get("channel_url") or data.get("webpage_url"),
        },
        "videos": videos,
    }


def _build_insights(videos: list) -> dict:
    views_list = [v["views"] for v in videos if isinstance(v.get("views"), (int, float))]
    avg_views = round(sum(views_list) / len(views_list)) if views_list else 0
    top = max(views_list and videos, key=lambda v: v.get("views") or -1) if views_list else None

    durations = [v["duration"] for v in videos if isinstance(v.get("duration"), (int, float))]
    if not durations:
        cadence_note = (
            f"Analyzed the {len(videos)} most recent videos; duration data was not "
            "available, so a cadence estimate can't be made from this sample."
        )
    else:
        avg_min = sum(durations) / len(durations) / 60
        if avg_min < 3:
            cadence_note = (
                f"Short-form leaning channel — the latest {len(videos)} videos average "
                f"{avg_min:.1f} min. Typical of Shorts/clip-style faceless channels; "
                "volume and hooks matter more than depth here."
            )
        elif avg_min < 12:
            cadence_note = (
                f"Mid-length format — the latest {len(videos)} videos average {avg_min:.1f} min, "
                "a proven sweet spot for faceless documentary/explainer content with strong retention."
            )
        else:
            cadence_note = (
                f"Long-form channel — the latest {len(videos)} videos average {avg_min:.1f} min. "
                "Long-form builds watch-time but needs strong retention hooks every 2–3 minutes."
            )

    return {
        "video_count": len(videos),
        "avg_views": avg_views,
        "top_video": (
            {"title": top["title"], "views": top["views"], "url": top["url"]} if top else None
        ),
        "cadence_note": cadence_note,
    }


def _run_analyze_job(job_id: str, url: str, max_videos: int, cache_key: str) -> None:
    _set_job(job_id, status="running")
    try:
        # A video link resolves to its channel first — any video URL works.
        url = _resolve_to_channel_url(url)
        cmd = [
            sys.executable, "-m", "yt_dlp",
            "--flat-playlist", "--dump-single-json",
            "--playlist-end", str(max_videos),
            "--extractor-args", "youtube:player_client=android",
            "--no-warnings",
            url,
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=240)
        if proc.returncode != 0:
            tail = (proc.stderr or "").strip().splitlines()
            hint = tail[-1] if tail else "yt-dlp failed"
            raise RuntimeError(
                "Could not read that channel — it may be private, age-restricted, "
                f"or the URL is wrong. ({hint[:160]})"
            )
        data = json.loads(proc.stdout or "{}")
        result = _parse_analyze(data, max_videos)
        # Prefer the human @handle from the submitted URL when present.
        m = re.search(r"youtube\.com/@([^/?#]+)", url)
        if m:
            result["channel"]["handle"] = "@" + m.group(1)
        if not result["videos"]:
            raise RuntimeError(
                "No public videos found at that URL. Check the link and try again."
            )
        # Flat-playlist entries don't carry view counts — enrich best-effort,
        # then compute insights from the enriched videos.
        _enrich_views(result["videos"])
        result["insights"] = _build_insights(result["videos"])
        # Alias the frontend reads for the stats row:
        result["stats"] = result["insights"]
        cache_put("analyze", cache_key, result, ttl=3600)
        _set_job(job_id, status="done", result=result)
        logger.info("Analyze job %s done: %d videos", job_id, len(result["videos"]))
    except subprocess.TimeoutExpired:
        _set_job(job_id, status="error",
                 error="The channel took too long to read. Try again with fewer videos.")
    except Exception as exc:
        logger.error("Analyze job %s failed: %s", job_id, type(exc).__name__)
        _set_job(job_id, status="error",
                 error=str(exc) if isinstance(exc, RuntimeError) else
                 "Something went wrong while analyzing. Please try again.")


@app.post("/api/jobs/analyze")
async def analyze(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"ok": False, "error": "Invalid JSON body."})
    url = str(body.get("youtube_url", "")).strip()
    max_videos = body.get("max_videos", 10)
    if not isinstance(max_videos, int) or not (5 <= max_videos <= 50):
        return JSONResponse(
            status_code=400,
            content={"ok": False, "error": "max_videos must be an integer between 5 and 50."},
        )
    if not url or not YOUTUBE_URL_RE.match(url):
        return JSONResponse(
            status_code=400,
            content={"ok": False,
                     "error": "That doesn't look like a YouTube channel/video/playlist URL "
                              "(expected youtube.com/@handle, /channel/, /c/, watch or playlist link)."},
        )

    ip = get_client_ip(request)
    if rate_limited((ip, "jobs/analyze"), 5):
        return _rate_limit_response(5)

    normalized = url.rstrip("/").lower()
    cache_key = f"{normalized}|{max_videos}"
    cached = cache_get("analyze", cache_key)
    job_id = _new_job("analyze")
    if cached is not None:
        _set_job(job_id, status="done", result=cached)
        return JSONResponse(status_code=202, content={"job_id": job_id})

    thread = threading.Thread(
        target=_run_analyze_job, args=(job_id, url, max_videos, cache_key), daemon=True
    )
    thread.start()
    return JSONResponse(status_code=202, content={"job_id": job_id})


@app.get("/api/jobs/{job_id}")
async def job_status(job_id: str):
    job = _get_job(job_id)
    if job is None:
        return JSONResponse(status_code=404, content={"error": "Job not found."})
    return {"status": job["status"], "result": job["result"], "error": job["error"],
            "progress": job.get("progress")}


@app.get("/api/jobs/{job_id}/download")
async def job_download(job_id: str):
    job = _get_job(job_id)
    if job is None:
        return JSONResponse(status_code=404, content={"error": "Job not found."})
    if job["kind"] not in ("analyze", "full") or job["status"] != "done":
        return JSONResponse(
            status_code=400,
