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
