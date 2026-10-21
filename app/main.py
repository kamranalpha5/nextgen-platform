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
    Failures are ignored — views simply stay None.
    """
    def fetch(video: dict) -> None:
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "yt_dlp",
                 "--no-playlist", "--no-warnings",
                 "--print", "%(view_count)s",
                 "--print", "%(upload_date)s",
                 "--extractor-args", "youtube:player_client=android",
                 video["url"]],
                capture_output=True, text=True, timeout=timeout_per,
            )
            out = [ln.strip() for ln in (proc.stdout or "").strip().splitlines()]
            if out and out[0].isdigit():
                video["views"] = int(out[0])
            if len(out) > 1 and len(out[1]) == 8 and out[1].isdigit() \
                    and not video.get("upload_date"):
                video["upload_date"] = out[1]
                video["date"] = f"{out[1][:4]}-{out[1][4:6]}-{out[1][6:8]}"
        except Exception:
            pass

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
            content={"ok": False, "error": "Download is only available for completed analysis jobs."},
        )
    payload = json.dumps(job["result"], indent=2, ensure_ascii=False).encode("utf-8")
    fname = "channel_channel_data.json" if job["kind"] == "full" else "channel-analysis.json"
    return FileResponse(
        path=_write_temp_download(job_id, payload),
        media_type="application/json",
        filename=fname,
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


def _write_temp_download(job_id: str, payload: bytes) -> str:
    tmp = DATA_DIR / f"download-{job_id}.json"
    tmp.write_bytes(payload)
    return str(tmp)


# ----------------------------------------------------------------------------
# POST /api/jobs/analyze-full — deep channel export
# ----------------------------------------------------------------------------
# Produces the full research package for a channel:
#   metadata    {channel_name, channel_id, subscribers, total_views, video_count}
#   videos[]    {video_id, title, published_at, views, likes, comments, duration}
#               for the last N (50–100) videos — views + dates included
#   outliers[]  videos performing far above the channel median
#   transcripts[]  full caption text for the top-5 and bottom-3 videos by views
#   transcript_status {requested, fetched, warning}
#   top_comments[]   top comments on the best videos, next-film requests flagged
#   next_film_requests[]  flattened audience asks for a next film
# ----------------------------------------------------------------------------

def _iso_duration(seconds) -> str | None:
    """Seconds -> ISO-8601 duration (PT#H#M#S), YouTube API style."""
    if not isinstance(seconds, (int, float)) or seconds < 0:
        return None
    total = int(round(seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    out = "PT"
    if h:
        out += f"{h}H"
    if m:
        out += f"{m}M"
    if s or out == "PT":
        out += f"{s}S"
    return out


def _iso_published(entry: dict) -> str | None:
    """yt-dlp entry -> ISO-8601 UTC timestamp (YYYY-MM-DDTHH:MM:SSZ)."""
    ts = entry.get("timestamp")
    if isinstance(ts, (int, float)) and ts > 0:
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    ud = entry.get("upload_date")
    if ud and len(str(ud)) == 8 and str(ud).isdigit():
        return f"{ud[:4]}-{ud[4:6]}-{ud[6:8]}T00:00:00Z"
    return None


def _ytdlp_info(url: str, extra_args: list, timeout: int = 60) -> dict | None:
    """Run yt-dlp -J for one URL; parsed dict or None on any failure."""
    cmd = [sys.executable, "-m", "yt_dlp", "--no-playlist", "--no-warnings",
           "-J"] + extra_args + [url]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if proc.returncode != 0 or not proc.stdout:
            return None
        return json.loads(proc.stdout)
    except Exception:
        return None


def _fetch_video_full(video_id: str) -> dict | None:
    url = f"https://www.youtube.com/watch?v={video_id}"
    info = _ytdlp_info(url, ["--extractor-args", "youtube:player_client=android"],
                       timeout=45)
    if info is None:  # fall back to the default client once
        info = _ytdlp_info(url, [], timeout=45)
    return info


_NEXT_FILM_RE = re.compile(
    r"(next\s+(film|movie|video|part|episode|chapter)|part\s*(2|two|ii)\b|sequel|"
    r"another\s+(one|film|movie|video)|please\s+make|make\s+(a\s+)?(next|another)|"
    r"waiting\s+for|continue\s+(the\s+)?(story|film|movie)|more\s+like\s+this|"
    r"next\s+one|bring\s+.*\s+back)",
    re.IGNORECASE,
)


def _fetch_top_comments(video_id: str, limit: int = 30) -> list:
    url = f"https://www.youtube.com/watch?v={video_id}"
    info = _ytdlp_info(
        url,
        ["--extractor-args",
         f"youtube:comment_sort=top;max_comments={limit * 2}"],
        timeout=90,
    )
    if not info:
        return []
    out = []
    for c in (info.get("comments") or []):
        text = (c.get("text") or "").strip()
        if not text:
            continue
        out.append({
            "author": c.get("author") or "",
            "text": text[:600],
            "likes": c.get("like_count") or 0,
            "is_next_film_request": bool(_NEXT_FILM_RE.search(text)),
        })
        if len(out) >= limit:
            break
    return out


def _fetch_transcript(video_id: str) -> str | None:
    """Best-effort caption text (manual or auto subs, English first)."""
    import html
    import tempfile
    url = f"https://www.youtube.com/watch?v={video_id}"
    with tempfile.TemporaryDirectory() as td:
        cmd = [sys.executable, "-m", "yt_dlp", "--skip-download", "--no-playlist",
               "--no-warnings", "--write-auto-subs", "--write-subs",
               "--sub-langs", "en.*", "--sub-format", "vtt",
               "--extractor-args", "youtube:player_client=android",
               "-o", os.path.join(td, "%(id)s.%(ext)s"), url]
        try:
            subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        except Exception:
            return None
        vtts = sorted(Path(td).glob("*.vtt"))
        if not vtts:
            return None
        raw = vtts[0].read_text(encoding="utf-8", errors="ignore")
    lines, seen_last = [], None
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith(("WEBVTT", "Kind:", "Language:", "NOTE")):
            continue
        if "-->" in line or re.fullmatch(r"\d+", line):
            continue
        line = html.unescape(re.sub(r"<[^>]+>", "", line)).strip()
        if line and line != seen_last:
            lines.append(line)
            seen_last = line
    text = "\n".join(lines).strip()
    return text or None


_VIDEO_URL_RE = re.compile(
    r"(?:youtube\.com/(?:watch\?[^#]*v=|shorts/|live/|embed/|v/)|youtu\.be/)"
    r"([A-Za-z0-9_-]{6,})"
)


def _resolve_to_channel_url(url: str) -> str:
    """Video link -> that video's channel /videos page; other URLs pass through.

    Lets the analyzer accept any video link (watch, Shorts, youtu.be, live,
    embed) and still return the COMPLETE channel JSON, not just that video.
    """
    m = _VIDEO_URL_RE.search(url or "")
    if not m:
        return url
    vid = m.group(1)
    watch = f"https://www.youtube.com/watch?v={vid}"
    info = _ytdlp_info(watch, ["--extractor-args", "youtube:player_client=android"],
                       timeout=45)
    if not (info or {}).get("channel_id") and not (info or {}).get("channel_url"):
        alt = _ytdlp_info(watch, [], timeout=45)
        if alt:
            info = alt
    if info:
        cid = info.get("channel_id")
        if cid:
            return f"https://www.youtube.com/channel/{cid}/videos"
        curl = info.get("channel_url") or info.get("uploader_url")
        if curl:
            return curl.rstrip("/") + "/videos"
    raise RuntimeError(
        "Could not find the channel for that video link. "
        "Check the link and try again."
    )


def _run_full_job(job_id: str, url: str, max_videos: int, cache_key: str) -> None:
    _set_job(job_id, status="running")
    try:
        # A video link resolves to its channel first — any video URL works.
        url = _resolve_to_channel_url(url)
        # 1) Flat channel fetch — ids + channel-level metadata.
        videos_url = url.rstrip("/")
        if not re.search(r"/(videos|streams|shorts)$", videos_url):
            videos_url += "/videos"
        cmd = [sys.executable, "-m", "yt_dlp", "--flat-playlist",
               "--dump-single-json", "--playlist-end", str(max_videos),
               "--extractor-args", "youtube:player_client=android",
               "--no-warnings", videos_url]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=240)
        if proc.returncode != 0:
            tail = (proc.stderr or "").strip().splitlines()
            hint = tail[-1] if tail else "yt-dlp failed"
            raise RuntimeError(
                "Could not read that channel — it may be private, age-restricted, "
                f"or the URL is wrong. ({hint[:160]})"
            )
        data = json.loads(proc.stdout or "{}")
        entries = _collect_video_entries(data, max_videos)
        ids, seen = [], set()
        for e in entries:
            vid = e.get("id")
            if vid and vid not in seen and e.get("_type") != "playlist":
                seen.add(vid)
                ids.append(vid)
        if not ids:
            raise RuntimeError("No public videos found at that URL. Check the link and try again.")

        channel_name = data.get("channel") or data.get("uploader") or ""
        channel_id = data.get("channel_id") or data.get("id") or ""
        subscribers = data.get("channel_follower_count")
        total_on_channel = None
        for e in (data.get("entries") or []):
            if isinstance(e, dict) and isinstance(e.get("playlist_count"), int):
                total_on_channel = e["playlist_count"]
                break

        # 2) Full per-video extraction in parallel (views, dates, likes...).
        _set_job(job_id, status="running", progress=f"Fetching details for {len(ids)} videos…")
        infos: dict = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            for vid, info in zip(ids, pool.map(_fetch_video_full, ids)):
                if info:
                    infos[vid] = info

        videos = []
        for vid in ids:
            info = infos.get(vid)
            if not info:
                continue
            videos.append({
                "video_id": vid,
                "title": info.get("title") or "Untitled",
                "published_at": _iso_published(info),
                "views": info.get("view_count"),
                "likes": info.get("like_count"),
                "comments": info.get("comment_count"),
                "duration": _iso_duration(info.get("duration")),
                "_views_sort": info.get("view_count") or 0,
            })
        videos.sort(key=lambda v: v["published_at"] or "", reverse=True)
        if not videos:
            raise RuntimeError("Could not fetch video details for that channel. Please try again.")

        total_views = sum(v["views"] for v in videos if isinstance(v.get("views"), int))
        metadata = {
            "channel_name": channel_name,
            "channel_id": channel_id,
            "subscribers": subscribers,
            "total_views": total_views,
            "video_count": total_on_channel if total_on_channel else len(videos),
        }
        
        # 3) Outliers vs the channel median.
        import statistics
        view_vals = sorted(v["_views_sort"] for v in videos if v["_views_sort"] > 0)
        median_views = statistics.median(view_vals) if view_vals else 0
        outliers = []
        if median_views:
            for v in sorted(videos, key=lambda x: x["_views_sort"], reverse=True):
                ratio = v["_views_sort"] / median_views
                if ratio >= 2 and v["_views_sort"] > 0:
                    outliers.append({
                        "video_id": v["video_id"], "title": v["title"],
                        "views": v["views"], "published_at": v["published_at"],
                        "multiple_of_median": round(ratio, 1),
                    })

        # 4) Transcripts — top 5 + bottom 3 by views.
        by_views = sorted(videos, key=lambda x: x["_views_sort"], reverse=True)
        picked, picked_ids = [], set()
        for v in by_views[:5] + by_views[-3:]:
            if v["video_id"] not in picked_ids:
                picked_ids.add(v["video_id"])
                picked.append(v)
        _set_job(job_id, status="running",
                 progress=f"Fetching transcripts for {len(picked)} videos…")
        transcripts = []
        for v in picked:
            text = _fetch_transcript(v["video_id"])
            if text:
                transcripts.append({
                    "title": v["title"], "video_id": v["video_id"], "text": text,
                })
        transcript_status = {"requested": len(picked), "fetched": len(transcripts)}
        if len(transcripts) < len(picked):
            transcript_status["warning"] = (
                f"Fetched {len(transcripts)}/{len(picked)} transcripts — "
                "some videos have no public captions."
            )

        # 5) Top comments on the 3 best videos + next-film requests.
        _set_job(job_id, status="running", progress="Fetching top comments…")
        top_comments, next_film_requests = [], []
        for v in by_views[:3]:
            comments = _fetch_top_comments(v["video_id"], limit=30)
            if comments:
                top_comments.append({
                    "video_id": v["video_id"], "title": v["title"],
                    "comments": comments,
                })
                for c in comments:
                    if c["is_next_film_request"]:
                        next_film_requests.append({
                            "video_id": v["video_id"], "video_title": v["title"],
                            "author": c["author"], "text": c["text"],
                            "likes": c["likes"],
                        })

        for v in videos:
            v.pop("_views_sort", None)
        result = {
            "metadata": metadata,
            "videos": videos,
            "outliers": outliers,
            "median_views": int(median_views) if median_views else None,
            "transcripts": transcripts,
            "transcript_status": transcript_status,
            "top_comments": top_comments,
            "next_film_requests": next_film_requests,
        }
        if not top_comments:
            result["comments_note"] = (
                "YouTube did not return comments for these videos — it blocks "
                "comment access from server IPs without sign-in. Views, dates, "
                "outliers and transcripts above are complete."
            )
        cache_put("analyze-full", cache_key, result, ttl=3600)
        _set_job(job_id, status="done", result=result)
        logger.info("Full job %s done: %d videos, %d transcripts",
                    job_id, len(videos), len(transcripts))
    except subprocess.TimeoutExpired:
        _set_job(job_id, status="error",
                 error="The channel took too long to read. Try again with fewer videos.")
    except Exception as exc:
        logger.error("Full job %s failed: %s", job_id, type(exc).__name__)
        _set_job(job_id, status="error",
                 error=str(exc) if isinstance(exc, RuntimeError) else
                 "Something went wrong during the full analysis. Please try again.")


@app.post("/api/jobs/analyze-full")
async def analyze_full(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"ok": False, "error": "Invalid JSON body."})
    url = str(body.get("youtube_url", "")).strip()
    max_videos = body.get("max_videos", 50)
    if not isinstance(max_videos, int) or not (10 <= max_videos <= 100):
        return JSONResponse(
            status_code=400,
            content={"ok": False, "error": "max_videos must be an integer between 10 and 100."},
        )
    if not url or not YOUTUBE_URL_RE.match(url):
        return JSONResponse(
            status_code=400,
            content={"ok": False,
                     "error": "That doesn't look like a YouTube channel URL "
                              "(expected youtube.com/@handle, /channel/ or /c/ link)."},
        )

    ip = get_client_ip(request)
    if rate_limited((ip, "jobs/analyze-full"), 3):
        return _rate_limit_response(3)

    normalized = url.rstrip("/").lower()
    cache_key = f"{normalized}|{max_videos}"
    cached = cache_get("analyze-full", cache_key)
    job_id = _new_job("full")
    if cached is not None:
        _set_job(job_id, status="done", result=cached)
        return JSONResponse(status_code=202, content={"job_id": job_id})

    thread = threading.Thread(
        target=_run_full_job, args=(job_id, url, max_videos, cache_key), daemon=True
    )
    thread.start()
    return JSONResponse(status_code=202, content={"job_id": job_id})


# ----------------------------------------------------------------------------
# POST /api/jobs/script — AI scriptwriter (Hugging Face, template fallback)
# ----------------------------------------------------------------------------
SCRIPT_STYLES = ("documentary", "explainer", "history", "true_crime", "sports", "essay")
SCRIPT_TONES = ("dramatic", "educational", "casual", "mysterious")
HF_MODEL = "mistralai/Mistral-7B-Instruct-v0.3"
HF_URL = "https://router.huggingface.co/v1/chat/completions"


def _hf_generate_script(topic: str, style: str, tone: str, length_min: int) -> dict:
    """Try the Hugging Face chat-completions router. Raises on ANY failure."""
    system = (
        "You are a professional YouTube scriptwriter for faceless channels. "
        "Respond with ONLY valid JSON, no markdown fences, no commentary, "
        "with keys title, hook, beats (list of heading/narration/visual_note), "
        "and cta. Narration must be full spoken paragraphs."
    )
    user = (
        f"Write a {length_min}-minute {style} video script in a {tone} tone "
        f"about: {topic}. Include a catchy title, a 1-2 line hook, "
        f"{'3' if length_min <= 4 else '4' if length_min <= 10 else '5'} beats "
        "with narration and visual notes, and a closing call to action."
    )
    req = urllib.request.Request(
        HF_URL,
        data=json.dumps({
            "model": HF_MODEL,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": 3000,
            "temperature": 0.8,
        }).encode(),
        headers={
            "Authorization": f"Bearer {HF_API_TOKEN}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read().decode())
    content = data["choices"][0]["message"]["content"].strip()
    # Strip accidental code fences, then parse.
    if content.startswith("```"):
        content = re.sub(r"^```[a-zA-Z]*\n?", "", content)
        content = re.sub(r"\n?```$", "", content).strip()
    script = json.loads(content)
    if not all(k in script for k in ("title", "hook", "beats", "cta")):
        raise ValueError("HF response missing required script fields")
    if not isinstance(script["beats"], list) or not script["beats"]:
        raise ValueError("HF response has no beats")
    return script


# --- Template engine: deterministic, style/tone-adapted --------------------
def _pick(options: list, seed: str):
    h = int(hashlib.md5(seed.encode()).hexdigest(), 16)
    return options[h % len(options)]


_TITLES = {
    "documentary": ["{T}: The Story Nobody Told You", "Inside {T} — A Cinematic Deep Dive"],
    "explainer": ["{T} Explained in {M} Minutes", "Everything About {T}, Simply Explained"],
    "history": ["The Dark History of {T}", "{T}: What History Tried to Forget"],
    "true_crime": ["The Chilling Case of {T}", "{T}: The Story That Shocked Everyone"],
    "sports": ["{T}: The Greatest Story in Sports", "How {T} Changed the Game Forever"],
    "essay": ["{T} Is Deeper Than You Think — A Video Essay", "What {T} Reveals About All of Us"],
}

_HOOKS = {
    "dramatic": [
        "What you're about to hear about {t} will change the way you see it forever.",
        "Nobody talks about the dark side of {t} — until now.",
        "{T} looks harmless on the surface. What lies underneath is a very different story.",
    ],
    "educational": [
        "By the end of this video, you'll understand {t} better than ninety-nine percent of people.",
        "Let's break down {t} — clearly, simply, and without the fluff.",
        "Everything you thought you knew about {t}? Let's put it to the test.",
    ],
    "casual": [
        "Okay, so {t} is actually way more interesting than you think — here's why.",
        "I went down the rabbit hole on {t} so you don't have to. Buckle up.",
        "Real talk: {t} deserves way more attention than it gets, and here's the proof.",
    ],
    "mysterious": [
        "Something about {t} doesn't add up — and the deeper you look, the stranger it gets.",
        "They don't want you asking questions about {t}. So let's ask them.",
        "Behind the official story of {t} lies something nobody expected to find.",
    ],
}

_BEAT_HEADINGS = {
    "documentary": ["Setting the Scene", "The Hidden Layers", "The Human Cost", "The Turning Point", "What Remains"],
    "explainer": ["The Basics", "How It Really Works", "The Part Everyone Misses", "Why It Matters", "The Big Picture"],
    "history": ["The Beginning", "The Rise", "The Dark Chapter", "The Fall", "The Legacy"],
    "true_crime": ["The Victim", "The Investigation", "The Breakthrough", "The Trial", "The Aftermath"],
    "sports": ["The Underdog", "The Climb", "The Defining Moment", "The Price of Glory", "The Legacy"],
    "essay": ["The Surface", "The Contradiction", "The Deeper Pattern", "The Uncomfortable Truth", "The Reflection"],
}

_OPENERS = {
    "documentary": [
        "To understand {t}, you have to see it the way the cameras never show you — from the inside out.",
        "Every story about {t} starts in the same place: with a detail almost everyone overlooks.",
        "Strip away the headlines about {t}, and what you're left with is far more fascinating.",
    ],
    "explainer": [
        "Let's start with the simplest possible definition of {t} — then build from there.",
        "Most explanations of {t} overcomplicate it. Here's the version that actually makes sense.",
        "Before we go any deeper, let's make sure we're on the same page about what {t} really is.",
    ],
    "history": [
        "To understand {t}, we have to go back — long before it became what it is today.",
        "The story of {t} doesn't begin where the textbooks say it does.",
        "History remembers {t} one way. The truth is messier, stranger, and far more interesting.",
    ],
    "true_crime": [
        "It started, as these stories often do, with something that seemed completely ordinary about {t}.",
        "Nobody suspected a thing about {t} — and that was exactly the problem.",
        "The first sign that something was wrong with {t} was easy to miss. Almost everyone did.",
    ],
    "sports": [
        "Every legend has an origin story, and the one behind {t} is wilder than you think.",
        "Long before the trophies and the headlines, {t} was just a long shot nobody believed in.",
        "Ask anyone about {t} today and they'll tell you the ending. Almost nobody knows the beginning.",
    ],
    "essay": [
        "On the surface, {t} seems simple enough. It isn't — and that's the point.",
        "We talk about {t} constantly, yet we rarely stop to ask what it actually means.",
        "There's a question hiding inside {t} that nobody wants to answer honestly.",
    ],
}

_MIDDLES = {
    "documentary": [
        "The deeper researchers dug into {t}, the more the official narrative started to crack.",
        "What makes {t} so compelling isn't the spectacle — it's the quiet moments nobody filmed.",
        "Experts who have spent decades studying {t} still disagree about what it all means.",
        "Follow the money, follow the power, follow the silence — and {t} starts to make sense.",
        "The people closest to {t} describe it in terms that never make it into the documentaries.",
        "Every frame of archival footage about {t} hides a detail that changes the story.",
        "What the cameras captured about {t} is only half the story — the rest was whispered.",
        "New evidence about {t} keeps surfacing, and each piece rewrites what came before.",
        "The official timeline of {t} has gaps — and the gaps are where the truth lives.",
        "Nobody who witnessed {t} firsthand ever described it the same way twice.",
    ],
    "explainer": [
        "Here's the mechanism behind {t}, step by step, with nothing skipped.",
        "Think of {t} like this: once you see the pattern, you can't unsee it.",
        "The key insight about {t} is surprisingly simple — but it changes everything.",
        "Most people get {t} wrong because they start from the wrong assumption.",
        "Let's test this with a concrete example of {t} in the real world.",
        "And this is where {t} gets genuinely interesting — the part textbooks gloss over.",
        "Once you grasp this one idea, the rest of {t} falls into place naturally.",
        "Here's a quick mental model for {t} you can use forever.",
        "The exceptions to the rule reveal the most about how {t} truly works.",
        "If you remember one thing about {t}, make it this.",
    ],
    "history": [
        "The records from that era paint a picture of {t} that feels almost unreal today.",
        "What happened next would reshape {t} — and echo for generations.",
        "The people who lived through the story of {t} left accounts that still give historians chills.",
        "For years, the truth about {t} was buried — until one discovery changed everything.",
        "Power, ambition, and fear collided, and {t} would never be the same again.",
        "The documents tell one story about {t}; the survivors tell another.",
        "What the victors wrote about {t} served their interests — not the truth.",
        "A single letter, diary, or artifact about {t} rewrote the accepted narrative.",
        "The turning point in the story of {t} came down to one fateful decision.",
        "Ordinary people caught in the story of {t} showed extraordinary courage.",
    ],
    "true_crime": [
        "Investigators looking into {t} quickly realized nothing was what it seemed.",
        "The evidence didn't add up — and the gaps in the story of {t} were the loudest part.",
        "Witnesses described {t} in ways that contradicted each other completely.",
        "Then came the breakthrough: a single detail about {t} that cracked the case open.",
        "The deeper the investigation into {t} went, the darker the picture became.",
        "To this day, experts still debate what really happened with {t}.",
        "A witness came forward years later with a detail about {t} that changed everything.",
        "The forensics told a story about {t} that nobody wanted to believe.",
        "Every alibi in the story of {t} had a crack — investigators just had to find it.",
        "The motive behind {t} was hiding in plain sight the entire time.",
    ],
    "sports": [
        "The training, the sacrifices, the setbacks — the road behind {t} was brutal.",
        "Critics wrote off {t} more times than anyone can count. They were wrong every time.",
        "What separated {t} from everyone else wasn't talent — it was something harder to measure.",
        "The moment everything changed for {t} happened in front of millions, in seconds.",
        "Behind every highlight of {t} there are a hundred hours nobody ever sees.",
        "Rivals still talk about facing {t} like it was a force of nature.",
        "The stadium fell silent the moment {t} proved every doubter wrong.",
        "Teammates say the work ethic behind {t} bordered on obsession.",
        "One play, one race, one moment — that's all it took to define {t}.",
        "The comeback story of {t} is the stuff Hollywood wishes it could invent.",
    ],
    "essay": [
        "The paradox at the heart of {t} is this: the more we discuss it, the less we understand it.",
        "Look closely at {t} and you'll see our own reflection staring back.",
        "We tell ourselves comforting stories about {t} — but the data tells a different one.",
        "The uncomfortable truth about {t} is that it reveals more about us than about itself.",
        "Every generation reinterprets {t}, and every generation thinks it finally got it right.",
        "What if everything we assume about {t} is backwards? Let's find out.",
        "The language we use to describe {t} shapes what we're allowed to conclude.",
        "Strip away the noise around {t} and a simpler, harder truth remains.",
        "We consume stories about {t} the way we consume everything: too fast to digest.",
        "Perhaps the real question about {t} is why we're so afraid to ask it.",
    ],
}

_CLOSERS = {
    "documentary": [
        "And that is the real story of {t} — not the myth, but the truth underneath it.",
        "The cameras moved on from {t} long ago. The story didn't end, though.",
        "What happens next with {t} is still being written — and it's worth watching.",
    ],
    "explainer": [
        "So that's {t} — demystified, step by step, with nothing left vague.",
        "Now that you see how {t} works, you'll notice it everywhere.",
        "And that's the whole picture of {t} — simple once you know where to look.",
    ],
    "history": [
        "History doesn't repeat with {t} — but it rhymes, if you're listening.",
        "The past never really stays buried when it comes to {t}.",
        "And so the story of {t} becomes a warning — and a lesson — for all of us.",
    ],
    "true_crime": [
        "Justice, in the story of {t}, was anything but simple.",
        "The case of {t} closed — but the questions never really did.",
        "Some stories about {t} end with answers. This one ends with a warning.",
    ],
    "sports": [
        "That's the legacy of {t} — not just wins, but what those wins cost.",
        "Long after the crowds go home, the story of {t} still inspires.",
        "Greatness fades. What {t} taught us doesn't.",
    ],
    "essay": [
        "Maybe {t} was never really about {t} at all.",
        "The question isn't what {t} means — it's what we're going to do about it.",
        "And that, in the end, is what {t} forces us to confront.",
    ],
}

_VISUALS = {
    "documentary": [
        "Slow cinematic B-roll related to {t}: shallow depth of field, muted grade, subtle film grain.",
        "Archival-style footage and maps connected to {t}, with slow push-in and lower-third labels.",
        "Aerial drone-style establishing shots evoking the world of {t}, cross-dissolve transitions.",
        "Close-up detail shots — textures, documents, faces — tied to {t}, with soft vignette.",
    ],
    "explainer": [
        "Clean motion-graphics diagram breaking down {t}, bold sans-serif labels, brand colors.",
        "Animated timeline/flowchart illustrating the key mechanism of {t}, step-by-step reveals.",
        "Split-screen comparison showing {t} versus the common misconception, with icons.",
        "Whiteboard-style sketch animation simplifying {t} into three visual chunks.",
    ],
    "history": [
        "Sepia-toned reenactment-style stills and paintings depicting the era of {t}, slow Ken Burns pan.",
        "Vintage maps and documents about {t}, camera drifting across aged paper textures.",
        "Dramatic silhouette imagery and fog overlays evoking the darker chapters of {t}.",
        "Timeline animation marking the key dates in the story of {t}, parchment background.",
    ],
    "true_crime": [
        "Dark, desaturated B-roll with crime-board aesthetics: red string, pinned photos about {t}.",
        "Night-time city footage with slow zoom, overlaid with case-file style text about {t}.",
        "Grainy, high-contrast imagery suggesting the investigation into {t}, flash-frame cuts.",
        "Map animation tracing movements and locations tied to {t}, with pulsing markers.",
    ],
    "sports": [
        "High-energy montage of athletic imagery tied to {t}: slow-motion, speed ramps, lens flares.",
        "Stadium crowd B-roll with rising audio swell, building toward the story of {t}.",
        "Training-ground footage — sweat, repetition, grit — mirroring the journey of {t}.",
        "Trophy-lift style hero shot composition celebrating the legacy of {t}, golden-hour grade.",
    ],
    "essay": [
        "Minimalist, contemplative B-roll — empty rooms, rain on glass — reflecting on {t}.",
        "Slow typography animation of the key question about {t}, generous negative space.",
        "Abstract visuals: ink in water, shifting shadows, mirroring the ambiguity of {t}.",
        "Quiet observational footage of everyday life, reframed by the argument about {t}.",
    ],
}

_CTAS = {
    "dramatic": "If this opened your eyes, subscribe — because the next story goes even deeper.",
    "educational": "Subscribe for clear, no-fluff breakdowns like this every single week.",
    "casual": "If you learned something, hit subscribe — way more rabbit holes where this came from.",
    "mysterious": "Subscribe — some stories are too strange to miss, and this was only the beginning.",
}


def _fill(template: str, topic: str) -> str:
    t = topic.strip()
    title_case = t[:1].upper() + t[1:] if t else t
    # Lowercase lead-in for mid-sentence slots, but never mangle acronyms
    # ("JFK assassination" must not become "jFK assassination").
    if len(t) > 1 and t[0].isupper() and t[1].islower():
        lower_lead = t[0].lower() + t[1:]
    else:
        lower_lead = t
    return template.replace("{T}", title_case).replace("{t}", lower_lead)


def template_script(topic: str, style: str, tone: str, length_min: int) -> dict:
    """Deterministic, style/tone-adapted script. Topic woven into every beat."""
    words_total = length_min * 140
    n_beats = 3 if length_min <= 4 else 4 if length_min <= 10 else 5
    words_per_beat = max(60, words_total // n_beats)
    headings = _BEAT_HEADINGS[style][:n_beats]

    title = _pick(_TITLES[style], f"{topic}|title").replace("{T}", topic.strip()).replace("{M}", str(length_min))
    hook = _pick(_HOOKS[tone], f"{topic}|hook|{tone}")
    hook = _fill(hook, topic)

    beats = []
    for i, heading in enumerate(headings):
        seed = f"{topic}|{style}|{i}"
        parts = [_fill(_pick(_OPENERS[style], seed + "|op"), topic)]
        # Add middle sentences until we hit the word target. Sentences are not
        # repeated within a beat until the bank is exhausted; after that,
        # reuse is allowed but never back-to-back.
        bank_size = len(_MIDDLES[style])
        used = set(parts)
        last = parts[-1]
        bank_exhausted = False
        j = 0
        guard = 0
        # Scale the per-beat sentence cap with the word target so long scripts
        # stay proportional (spaced reuse kicks in past the unique bank).
        max_middles = min(40, max(16, words_per_beat // 18))
        while sum(len(p.split()) for p in parts) < words_per_beat and j < max_middles and guard < 600:
            guard += 1
            if not bank_exhausted and len(used) > bank_size:
                bank_exhausted = True  # from here: reuse allowed, just never back-to-back
            cand = _fill(_pick(_MIDDLES[style], f"{seed}|mid|{j}|try{guard}"), topic)
            if cand == last or (not bank_exhausted and cand in used):
                continue
            used.add(cand)
            last = cand
            parts.append(cand)
            j += 1
        parts.append(_fill(_pick(_CLOSERS[style], seed + "|cl"), topic))
        narration = " ".join(parts)
        beats.append({
            "heading": f"{heading}: {topic.strip()}",
            "narration": narration,
            "visual_note": _fill(_pick(_VISUALS[style], seed + "|vis"), topic),
        })

    return {
        "title": title,
        "hook": hook,
        "beats": beats,
        "cta": _CTAS[tone],
    }

def _run_script_job(job_id: str, topic: str, style: str, tone: str, length_min: int) -> None:
    _set_job(job_id, status="running")
    engine = "template"
    try:
        if HF_API_TOKEN:
            try:
                script = _hf_generate_script(topic, style, tone, length_min)
                engine = "huggingface"
            except Exception as exc:
                # ANY HF failure falls back to the template engine.
                logger.warning("HF generation failed (%s); using template engine.", type(exc).__name__)
                script = template_script(topic, style, tone, length_min)
        else:
            script = template_script(topic, style, tone, length_min)
        words = " ".join(
            [script.get("hook", ""), script.get("cta", "")]
            + [b.get("narration", "") for b in script.get("beats", [])]
        ).split()
        script["estimated_words"] = len(words)
        _set_job(job_id, status="done", result={"script": script, "engine": engine})
        logger.info("Script job %s done (engine=%s)", job_id, engine)
    except Exception as exc:
        logger.error("Script job %s failed: %s", job_id, type(exc).__name__)
        _set_job(job_id, status="error", error="Could not generate the script. Please try again.")


@app.post("/api/jobs/script")
async def create_script(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"ok": False, "error": "Invalid JSON body."})
    topic = str(body.get("topic", "")).strip()
    style = str(body.get("style", "")).strip().lower()
    tone = str(body.get("tone", "")).strip().lower()
    length_min = body.get("length_min", 5)

    if not (3 <= len(topic) <= 200):
        return JSONResponse(
            status_code=400, content={"ok": False, "error": "topic must be 3–200 characters."}
        )
    if style not in SCRIPT_STYLES:
        return JSONResponse(
            status_code=400,
            content={"ok": False, "error": f"style must be one of: {', '.join(SCRIPT_STYLES)}."},
        )
    if tone not in SCRIPT_TONES:
        return JSONResponse(
            status_code=400,
            content={"ok": False, "error": f"tone must be one of: {', '.join(SCRIPT_TONES)}."},
        )
    if not isinstance(length_min, int) or not (1 <= length_min <= 30):
        return JSONResponse(
            status_code=400, content={"ok": False, "error": "length_min must be 1–30."}
        )

    ip = get_client_ip(request)
    if rate_limited((ip, "jobs/script"), 20):
        return _rate_limit_response(20)

    job_id = _new_job("script")
    threading.Thread(
        target=_run_script_job, args=(job_id, topic, style, tone, length_min), daemon=True
    ).start()
    return JSONResponse(status_code=202, content={"job_id": job_id})


# ----------------------------------------------------------------------------
# POST /api/jobs/voiceover — edge-tts MP3 synthesis
# ----------------------------------------------------------------------------
VOICES = [
    "en-US-AriaNeural",
    "en-US-GuyNeural",
    "en-US-JennyNeural",
    "en-GB-SoniaNeural",
    "en-US-DavisNeural",
    "en-US-AmberNeural",
]
RATE_RE = re.compile(r"^[+-]\d+%$")
PITCH_RE = re.compile(r"^[+-]\d+Hz$")


def _norm_rate(value) -> str | None:
    """Accept ints/floats/numeric strings (percent) or '+N%' strings → '+N%'."""
    try:
        if isinstance(value, str) and RATE_RE.match(value.strip()):
            return value.strip()
        n = int(float(str(value).strip().rstrip("%")))
    except (ValueError, TypeError):
        return None
    if not -100 <= n <= 100:
        return None
    return f"{'+' if n >= 0 else ''}{n}%"


def _norm_pitch(value) -> str | None:
    """Accept ints/floats/numeric strings (Hz) or '+NHz' strings → '+NHz'."""
    try:
        if isinstance(value, str) and PITCH_RE.match(value.strip()):
            return value.strip()
        n = int(float(str(value).strip().rstrip("Hz").rstrip("hz")))
    except (ValueError, TypeError):
        return None
    if not -100 <= n <= 100:
        return None
    return f"{'+' if n >= 0 else ''}{n}Hz"


# In proxied networks, aiohttp's trust_env proxy lookup is unreliable for
# websockets — pass the proxy URL explicitly to edge-tts when one is set.
# Unset in normal deployments (e.g. Render), where this stays None.
_TTS_PROXY = os.getenv("HTTPS_PROXY") or os.getenv("https_proxy") or None


def _run_voiceover_job(job_id: str, text: str, voice: str, rate: str, pitch: str) -> None:
    _set_job(job_id, status="running")
    out_path = DB_AUDIO_DIR / f"{job_id}.mp3"
    try:
        import edge_tts

        async def _synthesize():
            communicate = edge_tts.Communicate(
                text, voice, rate=rate, pitch=pitch, proxy=_TTS_PROXY
            )
            await communicate.save(str(out_path))

        asyncio.run(_synthesize())
        if not out_path.exists() or out_path.stat().st_size == 0:
            raise RuntimeError("TTS produced no audio.")
        _set_job(job_id, status="done",
                 result={"audio_url": f"/api/jobs/{job_id}/audio"})
        logger.info("Voiceover job %s done (%d bytes)", job_id, out_path.stat().st_size)
    except Exception as exc:
        logger.error("Voiceover job %s failed: %s", job_id, type(exc).__name__)
        _set_job(job_id, status="error",
                 error="Voiceover synthesis failed. Please try again.")


@app.post("/api/jobs/voiceover")
async def create_voiceover(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"ok": False, "error": "Invalid JSON body."})
    text = str(body.get("text", "")).strip()
    voice = str(body.get("voice", "en-US-AriaNeural")).strip()
    rate = _norm_rate(body.get("rate", "+0%"))
    pitch = _norm_pitch(body.get("pitch", "+0Hz"))

    if not (1 <= len(text) <= 3000):
        return JSONResponse(
            status_code=400, content={"ok": False, "error": "text must be 1–3000 characters."}
        )
    if voice not in VOICES:
        return JSONResponse(
            status_code=400,
            content={"ok": False, "error": f"voice must be one of: {', '.join(VOICES)}."},
        )
    if rate is None:
        return JSONResponse(
            status_code=400, content={"ok": False, "error": 'rate must be a percent between -100 and 100 (e.g. 10 or "+10%").'}
        )
    if pitch is None:
        return JSONResponse(
            status_code=400, content={"ok": False, "error": 'pitch must be Hz between -100 and 100 (e.g. 5 or "+5Hz").'}
        )

    ip = get_client_ip(request)
    if rate_limited((ip, "jobs/voiceover"), 10):
        return _rate_limit_response(10)

    job_id = _new_job("voiceover")
    threading.Thread(
        target=_run_voiceover_job, args=(job_id, text, voice, rate, pitch), daemon=True
    ).start()
    return JSONResponse(status_code=202, content={"job_id": job_id})


@app.get("/api/jobs/{job_id}/audio")
async def job_audio(job_id: str):
    job = _get_job(job_id)
    if job is None:
        return JSONResponse(status_code=404, content={"error": "Job not found."})
    if job["kind"] != "voiceover" or job["status"] != "done":
        return JSONResponse(
            status_code=400,
            content={"ok": False, "error": "Audio is only available for completed voiceover jobs."},
        )
    path = DB_AUDIO_DIR / f"{job_id}.mp3"
    if not path.exists():
        return JSONResponse(status_code=404, content={"error": "Audio file not found."})
    return FileResponse(str(path), media_type="audio/mpeg", filename=f"voiceover-{job_id}.mp3")


# ----------------------------------------------------------------------------
# Content endpoints — niches + resources
# ----------------------------------------------------------------------------
@app.get("/api/niches")
async def niches():
    try:
        data = json.loads(NICHE_FILE.read_text(encoding="utf-8"))
        return {"niches": data["niches"] if isinstance(data, dict) else data}
    except Exception as exc:
        logger.error("Failed to load niches.json: %s", type(exc).__name__)
        return JSONResponse(
            status_code=500, content={"ok": False, "error": "Could not load niches."}
        )


RESOURCES = {
    "hooks": [
        {"formula": "The curiosity gap",
         "example": "Nobody knows why this village vanished overnight — but the leading theory will chill you."},
        {"formula": "The bold claim",
         "example": "Everything you know about black holes is wrong. Here's the proof."},
        {"formula": "The forbidden knowledge",
         "example": "Banks don't want you to understand this one trick — which is exactly why you should."},
        {"formula": "The ticking clock",
         "example": "In 47 seconds, you'll understand why this plane never should have taken off."},
        {"formula": "The contrarian take",
         "example": "Motivation is useless. Here's what actually separates winners from everyone else."},
        {"formula": "The personal stake",
         "example": "This mistake cost me $4,000 — so you don't have to make it."},
        {"formula": "The unanswered question",
         "example": "A signal from deep space repeated 3 times, then stopped forever. Scientists are baffled."},
        {"formula": "The transformation promise",
         "example": "Watch this once and you'll never look at your sleep the same way again."},
    ],
    "titles": [
        {"formula": "Number + superlative + keyword",
         "example": "7 Disturbing Facts About the Ocean They Never Taught You"},
        {"formula": "How/Why + curiosity gap",
         "example": "Why Nobody Can Solve This 100-Year-Old Mystery"},
        {"formula": "The untold story angle",
         "example": "The Untold Story of the Heist That Broke Every Record"},
        {"formula": "Direct 'you' address",
         "example": "You're Charging Your Phone Wrong (Here's the Fix)"},
        {"formula": "Vs. comparison",
         "example": "Billionaire vs Millionaire Morning Routines — The Shocking Difference"},
        {"formula": "Time-bound urgency",
         "example": "What Happened in the Last 60 Seconds Before the Crash"},
        {"formula": "Myth-busting",
         "example": "The Titanic Myth Everyone Believes (It's False)"},
        {"formula": "Listicle with a twist",
         "example": "5 Places on Earth Science Still Can't Explain"},
    ],
    "checklists": {
        "pre_upload": [
            "Title under 60 characters with the keyword in the first 40",
            "Thumbnail text readable at 10% size (3 words max, high contrast)",
            "First 30 seconds restate the title's promise (no slow intros)",
            "End screen + pinned comment link to the next video in the series",
            "Chapters/timestamps added for videos over 8 minutes",
            "Description: 150+ words, keyword in first 2 lines, 3–5 hashtags",
            "Captions uploaded (not just auto-captions) for retention + SEO",
            "Premiere or scheduled at your audience's peak hour",
        ],
        "production": [
            "Script written to ~140 spoken words per minute of target length",
            "Hook (first 15 seconds) written before anything else",
            "One visual change every 4–7 seconds (B-roll, graphics, zooms)",
            "Voiceover recorded in one session for consistent tone",
            "Background music at -20dB under narration, never competing",
            "Pattern interrupts (questions, on-screen text) every 60–90 seconds",
            "Payoff delivered before the 80% mark — no saving it for the end",
            "CTA placed after the payoff, never before it",
        ],
    },
    "prompts": [
        "Write a 60-second hook for a faceless video about {topic} using the curiosity-gap formula.",
        "Turn this script beat into 3 B-roll search terms I can use on stock footage sites: {beat}",
        "Rewrite this narration to sound more dramatic without changing the facts: {text}",
        "Generate 10 title variations for a video about {topic}, each under 60 characters.",
        "Write a pinned comment that drives replies for a video about {topic}.",
        "Summarize this long article into a 5-beat video outline about {topic}: {article}",
    ],
}


@app.get("/api/resources")
async def resources():
    return RESOURCES


# ----------------------------------------------------------------------------
# POST /api/jobs/thumbnail-ai — script -> HD thumbnail via Gemini (free tier)
# ----------------------------------------------------------------------------
# Needs GEMINI_API_KEY (free from Google AI Studio: aistudio.google.com/apikey)
# in the environment. Two calls with the one key: a text model turns the
# script into a thumbnail concept + <=3-word on-image text, then an image
# model renders a 16:9 HD thumbnail. Without the key the job fails with a
# clear "not configured" message — the manual editor keeps working.
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip() or os.getenv("GOOGLE_API_KEY", "").strip()
GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
GEMINI_TEXT_MODEL = "gemini-2.0-flash"
GEMINI_IMAGE_MODELS = ("gemini-2.5-flash-image", "gemini-2.0-flash-preview-image-generation")

def _gemini_generate(model: str, payload: dict, timeout: int = 120) -> dict:
    req = urllib.request.Request(
        f"{GEMINI_BASE}/{model}:generateContent?key={GEMINI_API_KEY}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _gemini_concept(script_text: str) -> dict:
    """Script -> {image_prompt, thumbnail_text} via the Gemini text model."""
    instruction = (
        "You are a YouTube thumbnail designer. Read this video script and return "
        "ONLY valid JSON (no markdown fences) with two keys:\n"
        '- "thumbnail_text": the on-image text, MAX 3 words, ALL CAPS, punchy.\n'
        '- "image_prompt": a detailed image-generation prompt for a viral YouTube '
        "thumbnail: one dramatic main subject with an exaggerated emotional "
        "expression or a striking scene from the script, cinematic lighting, "
        "vivid saturated colors, ultra high contrast, clean composition with the "
        "subject on one side and copy space on the other, photorealistic, 16:9. "
        "The image itself must contain NO text, letters or words (text is added "
        "later by the editor).\n\nSCRIPT:\n"
    )
    data = _gemini_generate(GEMINI_TEXT_MODEL, {
        "contents": [{"parts": [{"text": instruction + script_text[:5000]}]}],
    })
    text = data["candidates"][0]["content"]["parts"][0]["text"].strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    concept = json.loads(text)
    if not concept.get("image_prompt"):
        raise ValueError("Gemini returned no image prompt")
    concept["thumbnail_text"] = str(concept.get("thumbnail_text", ""))[:40]
    return concept


def _gemini_image(image_prompt: str) -> tuple:
    """Render the thumbnail image; returns (base64_data, mime_type)."""
    last_exc = None
    for model in GEMINI_IMAGE_MODELS:
        for with_config in (True, False):
            payload = {"contents": [{"parts": [{"text": image_prompt}]}]}
            if with_config:
                payload["generationConfig"] = {
                    "responseModalities": ["TEXT", "IMAGE"],
                    "imageConfig": {"aspectRatio": "16:9"},
                }
            else:
                payload["generationConfig"] = {"responseModalities": ["TEXT", "IMAGE"]}
            try:
                data = _gemini_generate(model, payload, timeout=180)
                parts = data["candidates"][0]["content"]["parts"]
                for part in parts:
                    inline = part.get("inlineData") or part.get("inline_data")
                    if inline and inline.get("data"):
                        return inline["data"], inline.get("mimeType", "image/png")
                raise ValueError("Gemini returned no image")
            except Exception as exc:  # try the next model/config combo
                last_exc = exc
                continue
    raise RuntimeError(f"Gemini image generation failed ({type(last_exc).__name__}).")


def _run_thumb_job(job_id: str, script_text: str) -> None:
    _set_job(job_id, status="running")
    try:
        if not GEMINI_API_KEY:
            raise RuntimeError(
                "AI thumbnails are not configured yet. Add a free Gemini API key "
                "as GEMINI_API_KEY (Google AI Studio → Get API key; on Render: "
                "service → Environment) and try again. The manual editor below "
                "works without it."
            )
        _set_job(job_id, status="running", progress="Designing the thumbnail concept…")
        concept = _gemini_concept(script_text)
        _set_job(job_id, status="running", progress="Rendering the HD image…")
        b64, mime = _gemini_image(concept["image_prompt"])
        _set_job(job_id, status="done", result={
            "image_data_url": f"data:{mime};base64,{b64}",
            "thumbnail_text": concept["thumbnail_text"],
            "image_prompt": concept["image_prompt"],
            "engine": "gemini",
        })
        logger.info("Thumbnail job %s done", job_id)
    except Exception as exc:
        logger.error("Thumbnail job %s failed: %s", job_id, type(exc).__name__)
        _set_job(job_id, status="error",
                 error=str(exc) if isinstance(exc, RuntimeError) else
                 "Could not generate the thumbnail. Please try again.")


@app.post("/api/jobs/thumbnail-ai")
async def thumbnail_ai(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"ok": False, "error": "Invalid JSON body."})
    script_text = str(body.get("script", "")).strip()
    if not (30 <= len(script_text) <= 8000):
        return JSONResponse(
            status_code=400,
            content={"ok": False, "error": "script must be 30–8000 characters — paste your video script."},
        )
    ip = get_client_ip(request)
    if rate_limited((ip, "jobs/thumbnail-ai"), 10):
        return _rate_limit_response(10)
    job_id = _new_job("thumbnail")
    thread = threading.Thread(target=_run_thumb_job, args=(job_id, script_text), daemon=True)
    thread.start()
    return JSONResponse(status_code=202, content={"job_id": job_id})


# ----------------------------------------------------------------------------
# Library — saved scripts & analyses (auth required)
# ----------------------------------------------------------------------------
LIBRARY_TYPES = ("script", "analysis", "voiceover")
MAX_PAYLOAD_BYTES = 500 * 1024


@app.get("/api/library")
async def library_list(request: Request):
    email = get_session_email(request)
    if not email:
        return JSONResponse(status_code=401, content={"error": "unauthorized"})
    with _db_lock, _db() as conn:
        rows = conn.execute(
            "SELECT id, type, title, created_at FROM library WHERE email = ? ORDER BY id DESC",
            (email,),
        ).fetchall()
    return {"items": [dict(r) for r in rows]}


@app.get("/api/library/{item_id}")
async def library_get(item_id: int, request: Request):
    email = get_session_email(request)
    if not email:
        return JSONResponse(status_code=401, content={"error": "unauthorized"})
    with _db_lock, _db() as conn:
        row = conn.execute(
            "SELECT id, type, title, payload, created_at FROM library WHERE id = ? AND email = ?",
            (item_id, email),
        ).fetchone()
    if row is None:
        return JSONResponse(status_code=404, content={"error": "Item not found."})
    item = dict(row)
    item["payload"] = json.loads(item["payload"])
    return {"item": item}


@app.post("/api/library")
async def library_add(request: Request):
    email = get_session_email(request)
    if not email:
        return JSONResponse(status_code=401, content={"error": "unauthorized"})
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"ok": False, "error": "Invalid JSON body."})
    item_type = str(body.get("type", "")).strip()
    title = str(body.get("title", "")).strip()
    payload = body.get("payload")

    if item_type not in LIBRARY_TYPES:
        return JSONResponse(
            status_code=400, content={"ok": False, "error": "type must be 'script' or 'analysis'."}
        )
    if not (1 <= len(title) <= 200):
        return JSONResponse(
            status_code=400, content={"ok": False, "error": "title must be 1–200 characters."}
        )
    if not isinstance(payload, dict):
        return JSONResponse(
            status_code=400, content={"ok": False, "error": "payload must be a JSON object."}
        )
    payload_str = json.dumps(payload, ensure_ascii=False)
    if len(payload_str.encode("utf-8")) > MAX_PAYLOAD_BYTES:
        return JSONResponse(
            status_code=400, content={"ok": False, "error": "payload exceeds 500KB."}
        )

    with _db_lock, _db() as conn:
        if USE_POSTGRES:
            row = conn.execute(
                "INSERT INTO library (email, type, title, payload, created_at)"
                " VALUES (?, ?, ?, ?, ?) RETURNING id",
                (email, item_type, title, payload_str, now_ts()),
            ).fetchone()
            item_id = row["id"]
        else:
            cur = conn.execute(
                "INSERT INTO library (email, type, title, payload, created_at) VALUES (?, ?, ?, ?, ?)",
                (email, item_type, title, payload_str, now_ts()),
            )
            item_id = cur.lastrowid
    return {"ok": True, "id": item_id}


@app.delete("/api/library/{item_id}")
async def library_delete(item_id: int, request: Request):
    email = get_session_email(request)
    if not email:
        return JSONResponse(status_code=401, content={"error": "unauthorized"})
    with _db_lock, _db() as conn:
        cur = conn.execute(
            "DELETE FROM library WHERE id = ? AND email = ?", (item_id, email)
        )
    if cur.rowcount == 0:
        return JSONResponse(status_code=404, content={"error": "Item not found."})
    return {"ok": True}


# ----------------------------------------------------------------------------
# Health
# ----------------------------------------------------------------------------
@app.get("/api/health")
async def health():
    return {
        "ok": True,
        "demo_auth": DEMO_AUTH,
        "hf": bool(HF_API_TOKEN),
        "smtp": bool(SMTP_HOST),
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=PORT)
