/* ==========================================================================
   NextGen — vanilla-JS single-page app (no build step, no frameworks)
   --------------------------------------------------------------------------
   ROUTES (hash router):
     #/                  landing page
     #/signin            email + code sign-in
     #/app               dashboard home (requires login)
     #/app/home          dashboard home (alias)
     #/app/script       Script Studio
     #/app/voiceover     Voiceover Studio
     #/app/thumbnail     Thumbnail Studio (fully client-side)
     #/app/niches        Niche Screener
     #/app/analyzer      Channel Analyzer
     #/app/resources     Resources
     #/app/videos        My Videos

   BACKEND CONTRACT (coded against exactly):
     POST /api/auth/request-code  {"email"}            -> {"ok","demo","demo_code"}
     POST /api/auth/verify        {"email","code"}     -> {"ok","email"}
     POST /api/auth/logout                             -> {"ok":true}
     GET  /api/auth/me                                 -> {"email"} | 401
     POST /api/jobs/analyze       {"youtube_url","max_videos"} -> 202 {"job_id"}
     GET  /api/jobs/{id}          -> {"status","result","error"}
     GET  /api/jobs/{id}/download -> JSON file
     POST /api/jobs/script        {"topic","style","tone","length_min"} -> 202 {"job_id"}
     POST /api/jobs/voiceover     {"text","voice","rate","pitch"}       -> 202 {"job_id"}
     GET  /api/jobs/{id}/audio    -> mp3
     GET  /api/niches             -> {"niches":[...]}
     GET  /api/resources          -> {"hooks","titles","checklists","prompts"}
     GET  /api/library            -> {"items":[{"id","type","title","created_at"}]}
     GET  /api/library/{id}       -> {"item":{...}}
     POST /api/library            {"type","title","payload"} -> {"ok","id"}
     DELETE /api/library/{id}     -> {"ok":true}
     GET  /api/health             -> {"ok","demo_auth","hf","smtp"}
   ========================================================================== */
"use strict";

/* ------------------------------- Utils ---------------------------------- */

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

/** Escape user/remote content before injecting into HTML (XSS defense). */
function esc(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

/** Copy text to clipboard with a legacy fallback. Returns true on success. */
async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch (e) {
    // Fallback for non-secure contexts / older browsers.
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    let ok = false;
    try { ok = document.execCommand("copy"); } catch (_) { /* noop */ }
    ta.remove();
    return ok;
  }
}

/** Compact number formatting: 1234567 -> "1.2M". */
function fmtNum(n) {
  n = Number(n);
  if (!Number.isFinite(n)) return "—";
  if (n >= 1e9) return (n / 1e9).toFixed(1) + "B";
  if (n >= 1e6) return (n / 1e6).toFixed(1) + "M";
  if (n >= 1e3) return (n / 1e3).toFixed(1) + "K";
  return String(Math.round(n));
}

function fmtDate(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? String(iso) : d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
}

function fmtDur(sec) {
  sec = Number(sec);
  if (!Number.isFinite(sec) || sec < 0) return "—";
  sec = Math.round(sec);
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  return h ? `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}` : `${m}:${String(s).padStart(2, "0")}`;
}

/* ------------------------------- Toasts --------------------------------- */

/** Show a toast notification. type: "info" | "success" | "error" | "warning". */
function toast(msg, type = "info", ms = 4200) {
  const root = $("#toast-root");
  const el = document.createElement("div");
  el.className = "toast" + (type !== "info" ? " " + type : "");
  el.textContent = msg;
  root.appendChild(el);
  setTimeout(() => {
    el.style.opacity = "0";
    el.style.transition = "opacity 0.3s ease";
    setTimeout(() => el.remove(), 320);
  }, ms);
}

/* ------------------------------ API layer ------------------------------- */

const API_BASE = ""; // same origin; backend serves /api/* alongside this file.

/**
 * JSON fetch helper.
 * - Throws Error with the server's message on non-2xx (parsed from JSON `error`/`message`).
 * - On 401, redirects to #/signin and throws (so callers stop).
 */
async function api(path, { method = "GET", body = null } = {}) {
  const opts = {
    method,
    headers: { "Content-Type": "application/json" },
    credentials: "same-origin",
  };
  if (body !== null) opts.body = JSON.stringify(body);

  let res;
  try {
    res = await fetch(API_BASE + path, opts);
  } catch (e) {
    throw new Error("Network error — could not reach the server. Check your connection.");
  }

  if (res.status === 401) {
    // Session expired or not logged in: bounce to sign-in.
    if (!location.hash.startsWith("#/signin")) location.hash = "#/signin";
    throw new Error("Session expired — please sign in again.");
  }

  let data = null;
  const ct = res.headers.get("content-type") || "";
  if (ct.includes("application/json")) {
    try { data = await res.json(); } catch (_) { data = null; }
  }

  if (!res.ok) {
    const msg = (data && (data.error || data.message)) ||
      `Request failed (${res.status} ${res.statusText})`;
    const err = new Error(msg);
    err.status = res.status;
    err.data = data;
    throw err;
  }
  return data;
}

/**
 * POST a job and poll GET /api/jobs/{id} every 2s until done/error.
 * onTick(statusPayload) is called with each poll response for progress UI.
 * Resolves with the job's `result` on status "done".
 */
async function runJob(postPath, postBody, onTick) {
  const started = await api(postPath, { method: "POST", body: postBody });
  const jobId = started && started.job_id;
  if (!jobId) throw new Error("Server did not return a job_id.");

  const t0 = Date.now();
  const TIMEOUT_MS = 10 * 60 * 1000; // 10 min safety cap
  for (;;) {
    const job = await api(`/api/jobs/${encodeURIComponent(jobId)}`);
    if (onTick) onTick(job);
    const status = job && job.status;
    if (status === "done") return { jobId, result: job.result };
    if (status === "error") {
      throw new Error((job && job.error) || "The job failed on the server.");
    }
    if (Date.now() - t0 > TIMEOUT_MS) {
      throw new Error("Timed out waiting for the job. Please try again.");
    }
    await new Promise((r) => setTimeout(r, 2000));
  }
}

/* ------------------------------ Auth state ------------------------------ */

const Auth = {
  email: null,
  checked: false,
  /** Verify the current session (GET /api/auth/me). Cached per page load. */
  async me() {
    if (this.checked) return this.email;
    try {
      const data = await api("/api/auth/me");
      this.email = (data && data.email) || null;
    } catch (e) {
      this.email = null; // 401 handled inside api()
    }
    this.checked = true;
    return this.email;
  },
  reset() { this.email = null; this.checked = false; },
};

/* ------------------------------ Router ---------------------------------- */

const app = $("#app");

/** Tool registry: slug -> {title, icon, desc} used by sidebar, cards, mobile bar. */
const TOOLS = {
  script:    { title: "Script Studio",    icon: "📝", desc: "Turn any topic into a narrated, faceless-ready script with hooks, beats and visual notes." },
  voiceover: { title: "Voiceover Studio", icon: "🎙️", desc: "Convert your script into a natural AI voiceover and download the MP3." },
  thumbnail: { title: "Thumbnail Studio",  icon: "🖼️", desc: "Design click-worthy 1280×720 thumbnails right in your browser — no uploads needed." },
  niches:    { title: "Niche Screener",    icon: "🎯", desc: "Browse scored faceless niches with competition data, CPM notes and video ideas." },
  analyzer:  { title: "Channel Analyzer",  icon: "📊", desc: "Paste any YouTube channel URL and get stats, top videos and posting insights." },
  resources: { title: "Resources",         icon: "📚", desc: "Hook formulas, title formulas, checklists and starter prompts — one click to copy." },
  videos:    { title: "My Videos",         icon: "💾", desc: "Your saved scripts and channel analyses, ready to open or delete." },
};

const NAV_ORDER = ["home", "script", "voiceover", "thumbnail", "niches", "analyzer", "resources", "videos"];

/** Render the current hash route. */
async function route() {
  const hash = location.hash || "#/";
  window.scrollTo(0, 0);

  if (hash === "#/" || hash === "#") { renderLanding(); return; }
  if (hash.startsWith("#/signin")) { renderSignin(); return; }

  if (hash === "#/app" || hash === "#/app/" || hash === "#/app/home") {
    await requireAuth(() => renderDashboard("home", renderHome));
    return;
  }
  const m = hash.match(/^#\/app\/([a-z-]+)\/?$/);
  if (m && TOOLS[m[1]]) {
    const tool = m[1];
    const view = {
      script: renderScriptStudio,
      voiceover: renderVoiceoverStudio,
      thumbnail: renderThumbnailStudio,
      niches: renderNicheScreener,
      analyzer: renderChannelAnalyzer,
      resources: renderResources,
      videos: renderLibrary,
    }[tool];
    await requireAuth(() => renderDashboard(tool, view));
    return;
  }
  // Unknown route -> landing.
  renderLanding();
}

/** Gate: ensure logged in (GET /api/auth/me), else bounce to #/signin. */
async function requireAuth(renderFn) {
  app.innerHTML = `<div class="loading-block"><div class="spinner"></div><p>Checking your session…</p></div>`;
  const email = await Auth.me();
  if (!email) {
    location.hash = "#/signin";
    return;
  }
  renderFn();
}

window.addEventListener("hashchange", route);

/* ==========================================================================
   LANDING (#/)
   ========================================================================== */

function renderLanding() {
  const features = [
    { icon: "📝", title: "AI Script Studio", text: "Topic in, full narration script out — hooks, story beats and visual directions included." },
    { icon: "🎙️", title: "Voiceover Studio", text: "Six natural AI voices turn your script into studio-quality narration you can download as MP3." },
    { icon: "🖼️", title: "Thumbnail Studio", text: "Design 1280×720 thumbnails in your browser with layered text, presets and one-click PNG export." },
    { icon: "🎯", title: "Niche Screener", text: "Scored faceless niches with competition levels, CPM guidance and ready video ideas." },
    { icon: "📊", title: "Channel Analyzer", text: "Paste any channel URL and see real stats, top videos, upload cadence and growth insights." },
    { icon: "📚", title: "Creator Resources", text: "Proven hook formulas, title templates, production checklists and starter prompts." },
  ];

  const toolCards = Object.entries(TOOLS)
    .filter(([slug]) => slug !== "videos")
    .map(([slug, t]) => `
      <div class="card hoverable tool-card">
        <h3>${t.icon} ${esc(t.title)}</h3>
        <p>${esc(t.desc)}</p>
        <a class="go" href="#/signin">Try it free →</a>
      </div>`).join("");

  const faqs = [
    {
      q: "Is NextGen really free?",
      a: "Yes — 100% free, free forever. There is no pricing page, no paid tier and no credit card required. Sign in with your email and every tool is unlocked.",
    },
    {
      q: "Do I need to show my face or use my own voice?",
      a: "No. NextGen is built for faceless channels: the Script Studio writes narration-first scripts, the Voiceover Studio generates the narration audio, and the Thumbnail Studio designs your packaging. You never appear on camera.",
    },
    {
      q: "How does sign-in work?",
      a: "Enter your email and we send you a 6-digit code. Enter the code and you're in — no passwords to remember. If email delivery isn't configured on the server, you'll see a clearly-labeled demo box with your code on screen.",
    },
    {
      q: "Can I save my scripts and analyses?",
      a: "Yes. Every tool has a “Save to My Videos” button. Your library keeps scripts and channel analyses tied to your account so you can reopen, copy or delete them anytime.",
    },
    {
      q: "What video formats does this support?",
      a: "Scripts are generated for any length from 1 to 30 minutes and tuned for styles like documentary, explainer, history, true crime, sports and essays. Voiceovers download as MP3 and thumbnails export as 1280×720 PNG — ready for YouTube.",
    },
    {
      q: "Who owns the content I create?",
      a: "You do. Everything you generate — scripts, voiceovers, thumbnails — is yours to publish and monetize on your own channels.",
    },
  ];

  app.innerHTML = `
    <nav class="nav">
      <div class="nav-inner">
        <a class="logo" href="#/"><span class="logo-mark">N</span>NextGen</a>
        <div class="nav-links">
          <a class="nav-hide-m" href="#features">Features</a>
          <a class="nav-hide-m" href="#how">How it works</a>
          <a class="nav-hide-m" href="#tools">Tools</a>
          <a class="btn btn-primary btn-sm" href="#/signin">Sign in</a>
        </div>
      </div>
    </nav>

    <header class="hero wrap">
      <span class="pill">100% free · no credit card</span>
      <h1>Make faceless YouTube videos — <span class="grad-text">free forever</span></h1>
      <p class="lede">Scripts, AI voiceovers, thumbnails, niche research and channel analytics — the complete toolkit for running a faceless YouTube channel, all in one place.</p>
      <div class="hero-cta">
        <a class="btn btn-primary" href="#/signin">Start free</a>
        <a class="btn btn-ghost" href="#tools">Explore the tools</a>
      </div>
      <div class="hero-badges">
        <span class="tag">📝 Script Studio</span>
        <span class="tag">🎙️ Voiceover Studio</span>
        <span class="tag">🖼️ Thumbnail Studio</span>
        <span class="tag">🎯 Niche Screener</span>
        <span class="tag">📊 Channel Analyzer</span>
      </div>
    </header>

    <section class="section wrap" id="features">
      <div class="section-head">
        <h2>Everything a faceless channel needs</h2>
        <p>Six purpose-built tools that cover the full production pipeline — from idea to upload.</p>
      </div>
      <div class="grid-3">
        ${features.map(f => `
          <div class="card hoverable feature-card">
            <div class="icon">${f.icon}</div>
            <h3>${esc(f.title)}</h3>
            <p>${esc(f.text)}</p>
          </div>`).join("")}
      </div>
    </section>

    <section class="section wrap" id="how">
      <div class="section-head">
        <h2>How it works</h2>
        <p>From sign-in to your first video in three steps.</p>
      </div>
      <div class="steps">
        <div class="card step">
          <div class="step-num">1</div>
          <h3>Sign in with your email</h3>
          <p>No passwords, no credit card. Enter your email, type the 6-digit code we send you, and you're in.</p>
        </div>
        <div class="card step">
          <div class="step-num">2</div>
          <h3>Pick a tool and create</h3>
          <p>Generate a script, turn it into a voiceover, design the thumbnail, or research your niche — right in the dashboard.</p>
        </div>
        <div class="card step">
          <div class="step-num">3</div>
          <h3>Download and upload</h3>
          <p>Export scripts as text, voiceovers as MP3 and thumbnails as PNG. Upload to YouTube and grow — free forever.</p>
        </div>
      </div>
    </section>

    <section class="section wrap" id="tools">
      <div class="section-head">
        <h2>The toolkit</h2>
        <p>Sign in once — every tool below is included, free.</p>
      </div>
      <div class="grid-3">${toolCards}</div>
    </section>

    <section class="section wrap" id="faq">
      <div class="section-head">
        <h2>Frequently asked questions</h2>
      </div>
      <div class="faq">
        ${faqs.map((f, i) => `
          <div class="faq-item${i === 0 ? " open" : ""}">
            <button class="faq-q" data-faq="${i}">${esc(f.q)}<span class="chev">+</span></button>
            <div class="faq-a"><div class="faq-a-inner">${esc(f.a)}</div></div>
          </div>`).join("")}
      </div>
    </section>

    <footer class="footer">
      <div class="footer-inner">
        <a class="logo" href="#/"><span class="logo-mark">N</span>NextGen</a>
        <div class="footer-links">
          <a href="#features">Features</a>
          <a href="#how">How it works</a>
          <a href="#tools">Tools</a>
          <a href="#/signin">Sign in</a>
        </div>
        <span>© ${new Date().getFullYear()} NextGen — free forever.</span>
      </div>
    </footer>`;

  // FAQ accordion wiring.
  $$("[data-faq]", app).forEach((btn) => {
    btn.addEventListener("click", () => {
      const item = btn.closest(".faq-item");
      const wasOpen = item.classList.contains("open");
      $$(".faq-item.open", app).forEach((el) => el.classList.remove("open"));
      if (!wasOpen) item.classList.add("open");
    });
  });
}

/* ==========================================================================
   SIGN-IN (#/signin) — email code flow with DEMO MODE handling
   ========================================================================== */

function renderSignin() {
  // Already signed in? Skip straight to the app.
  Auth.me().then((email) => {
    if (email && (location.hash === "#/signin" || location.hash.startsWith("#/signin"))) {
      location.hash = "#/app";
    }
  });

  app.innerHTML = `
    <nav class="nav">
      <div class="nav-inner">
        <a class="logo" href="#/"><span class="logo-mark">N</span>NextGen</a>
        <div class="nav-links"><a href="#/">← Back to home</a></div>
      </div>
    </nav>
    <div class="auth-page">
      <div class="card auth-card">
        <div class="auth-steps">
          <div class="auth-step active" id="step-bar-1"></div>
          <div class="auth-step" id="step-bar-2"></div>
        </div>
        <h1>Sign in to NextGen</h1>
        <p class="sub">Free forever — no password, no credit card.</p>
        <div id="auth-error"></div>
        <div id="auth-body"></div>
      </div>
    </div>`;

  showEmailStep();
}

/** Step 1: email -> POST /api/auth/request-code */
function showEmailStep() {
  const body = $("#auth-body");
  $("#step-bar-1").classList.add("active");
  $("#step-bar-2").classList.remove("active");
  body.innerHTML = `
    <form id="email-form" novalidate>
      <div class="field">
        <label for="email-input">Email address</label>
        <input class="input" id="email-input" type="email" placeholder="you@example.com"
               autocomplete="email" required />
        <div class="hint">We'll send you a 6-digit sign-in code.</div>
      </div>
      <button class="btn btn-primary" id="send-code-btn" type="submit" style="width:100%">
        Send code
      </button>
    </form>`;

  $("#email-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const email = $("#email-input").value.trim();
    if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) {
      showAuthError("Please enter a valid email address.");
      return;
    }
    const btn = $("#send-code-btn");
    btn.disabled = true;
    btn.innerHTML = `<span class="spinner sm"></span>&nbsp;Sending…`;
    clearAuthError();
    try {
      // Contract: POST /api/auth/request-code {"email"} -> {"ok","demo","demo_code"}
      const data = await api("/api/auth/request-code", { method: "POST", body: { email } });
      if (!data || data.ok === false) {
        throw new Error((data && (data.error || data.message)) || "Could not send the code.");
      }
      const isDemo = data.demo === true;
      const demoCode = data.demo_code || data.code || null; // tolerate either field name
      showCodeStep(email, isDemo, demoCode);
      toast(isDemo ? "Demo mode — your code is shown on screen." : "Code sent — check your inbox.", "success");
    } catch (err) {
      showAuthError(err.message);
      btn.disabled = false;
      btn.textContent = "Send code";
    }
  });
}

/** Step 2: code -> POST /api/auth/verify. Shows DEMO MODE box when demo:true. */
function showCodeStep(email, isDemo, demoCode) {
  const body = $("#auth-body");
  $("#step-bar-1").classList.add("active");
  $("#step-bar-2").classList.add("active");
  body.innerHTML = `
    <div class="form-note">Code sent to <strong>${esc(email)}</strong>.
      <a href="javascript:void(0)" id="change-email" style="margin-left:6px">Use a different email</a>
    </div>
    ${isDemo && demoCode ? `
      <div class="demo-box" role="status">
        <div class="demo-label">⚠ Demo mode — email not configured</div>
        <div class="demo-code">${esc(demoCode)}</div>
        <p>Demo mode: email not configured — your code is <strong>${esc(demoCode)}</strong>. Enter it below to continue.</p>
      </div>` : ""}
    <form id="code-form" novalidate>
      <div class="field">
        <label for="code-input">6-digit code</label>
        <input class="input code-input" id="code-input" type="text" inputmode="numeric"
               maxlength="6" placeholder="••••••" autocomplete="one-time-code" required />
      </div>
      <button class="btn btn-primary" id="verify-btn" type="submit" style="width:100%">
        Verify &amp; sign in
      </button>
      <div class="center" style="margin-top:12px">
        <button type="button" class="btn btn-ghost btn-sm" id="resend-btn">Resend code</button>
      </div>
    </form>`;

  $("#change-email").addEventListener("click", showEmailStep);

  $("#resend-btn").addEventListener("click", async (e) => {
    const btn = e.currentTarget;
    btn.disabled = true;
    btn.textContent = "Sending…";
    try {
      const data = await api("/api/auth/request-code", { method: "POST", body: { email } });
      const isDemo2 = data.demo === true;
      const demoCode2 = data.demo_code || data.code || null;
      showCodeStep(email, isDemo2, demoCode2);
      toast("A new code was sent.", "success");
    } catch (err) {
      showAuthError(err.message);
      btn.disabled = false;
      btn.textContent = "Resend code";
    }
  });

  $("#code-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const code = $("#code-input").value.trim();
    if (!/^\d{4,8}$/.test(code)) {
      showAuthError("Enter the code you received (digits only).");
      return;
    }
    const btn = $("#verify-btn");
    btn.disabled = true;
    btn.innerHTML = `<span class="spinner sm"></span>&nbsp;Verifying…`;
    clearAuthError();
    try {
      // Contract: POST /api/auth/verify {"email","code"} -> {"ok","email"} (sets cookie)
      const data = await api("/api/auth/verify", { method: "POST", body: { email, code } });
      if (!data || data.ok === false) {
        throw new Error((data && (data.error || data.message)) || "Invalid code.");
      }
      Auth.email = (data && data.email) || email;
      Auth.checked = true;
      toast("Signed in — welcome to NextGen!", "success");
      location.hash = "#/app";
    } catch (err) {
      showAuthError(err.message);
      btn.disabled = false;
      btn.innerHTML = "Verify &amp; sign in";
    }
  });

  $("#code-input").focus();
}

function showAuthError(msg) {
  $("#auth-error").innerHTML = `<div class="form-error">${esc(msg)}</div>`;
}
function clearAuthError() {
  $("#auth-error").innerHTML = "";
}

/* ==========================================================================
   DASHBOARD SHELL (#/app …) — sidebar + mobile bottom nav + content area
   ========================================================================== */

function sideLink(slug, label, icon) {
  const href = slug === "home" ? "#/app" : `#/app/${slug}`;
  return `<a class="side-link" data-nav="${slug}" href="${href}">
    <span class="ico">${icon}</span><span>${esc(label)}</span></a>`;
}

/** Render the dashboard chrome, then call viewFn(mainEl) for page content. */
function renderDashboard(active, viewFn) {
  const email = Auth.email || "";
  const links = [
    sideLink("home", "Home", "🏠"),
    sideLink("script", "Script Studio", "📝"),
    sideLink("voiceover", "Voiceover Studio", "🎙️"),
    sideLink("thumbnail", "Thumbnail Studio", "🖼️"),
    sideLink("niches", "Niche Screener", "🎯"),
    sideLink("analyzer", "Channel Analyzer", "📊"),
    sideLink("resources", "Resources", "📚"),
    sideLink("videos", "My Videos", "💾"),
  ].join("");

  const mobileLinks = [
    ["home", "#/app", "🏠", "Home"],
    ["script", "#/app/script", "📝", "Scripts"],
    ["voiceover", "#/app/voiceover", "🎙️", "Voice"],
    ["thumbnail", "#/app/thumbnail", "🖼️", "Thumb"],
    ["niches", "#/app/niches", "🎯", "Niches"],
    ["analyzer", "#/app/analyzer", "📊", "Analyze"],
    ["videos", "#/app/videos", "💾", "Saved"],
  ].map(([slug, href, icon, label]) =>
    `<a data-nav="${slug}" href="${href}"><span class="ico">${icon}</span>${label}</a>`).join("");

  app.innerHTML = `
    <div class="dash">
      <aside class="sidebar">
        <a class="logo" href="#/app"><span class="logo-mark">N</span>NextGen</a>
        <div class="side-label">Create</div>
        ${links}
        <div class="side-foot">
          <div class="side-user" title="${esc(email)}">👤 ${esc(email)}</div>
          <a class="side-link" href="javascript:void(0)" id="logout-link">
            <span class="ico">🚪</span><span>Logout</span></a>
        </div>
      </aside>
      <main class="dash-main" id="dash-main"></main>
      <nav class="mobile-bar" aria-label="Tools">${mobileLinks}</nav>
    </div>`;

  // Highlight active nav in both sidebar and mobile bar.
  $$(`[data-nav="${active}"]`, app).forEach((el) => el.classList.add("active"));

  $("#logout-link").addEventListener("click", async () => {
    try { await api("/api/auth/logout", { method: "POST" }); }
    catch (e) { /* still log out locally */ }
    Auth.reset();
    toast("Signed out.", "info");
    location.hash = "#/";
  });

  viewFn($("#dash-main"));
}

/* ------------------------------ HOME ------------------------------------ */

function renderHome(main) {
  const cards = NAV_ORDER.filter((s) => s !== "home").map((slug) => {
    const t = TOOLS[slug];
    const href = slug === "videos" ? "#/app/videos" : `#/app/${slug}`;
    return `
      <div class="card hoverable tool-card-dash" data-goto="${href}" role="link" tabindex="0">
        <div class="icon">${t.icon}</div>
        <h3>${esc(t.title)}</h3>
        <p>${esc(t.desc)}</p>
      </div>`;
  }).join("");

  main.innerHTML = `
    <div class="dash-head">
      <h1>Welcome back 👋</h1>
      <p>Pick a tool below — everything is free, free forever.</p>
    </div>
    <div class="tool-grid">${cards}</div>
    <div class="strip">
      <span class="n">How to use →</span>
      <span class="muted small"><strong>1.</strong> Generate a script &nbsp;·&nbsp; <strong>2.</strong> Create the voiceover &nbsp;·&nbsp; <strong>3.</strong> Design the thumbnail &nbsp;·&nbsp; <strong>4.</strong> Save everything to My Videos</span>
    </div>`;

  $$("[data-goto]", main).forEach((el) => {
    const go = () => { location.hash = el.dataset.goto; };
    el.addEventListener("click", go);
    el.addEventListener("keydown", (e) => { if (e.key === "Enter") go(); });
  });
}

/* --------------------------- SCRIPT STUDIO ------------------------------ */

const SCRIPT_STYLES = [
  ["documentary", "Documentary"],
  ["explainer", "Explainer"],
  ["history", "History"],
  ["true_crime", "True Crime"],
  ["sports", "Sports"],
  ["essay", "Video Essay"],
];
const SCRIPT_TONES = [
  ["dramatic", "Dramatic"],
  ["educational", "Educational"],
  ["casual", "Casual"],
  ["mysterious", "Mysterious"],
];

function renderScriptStudio(main) {
  main.innerHTML = `
    <div class="dash-head">
      <h1>📝 Script Studio</h1>
      <p>Turn any topic into a narrated, faceless-ready script.</p>
    </div>
    <div class="panel">
      <form id="script-form" novalidate>
        <div class="field">
          <label for="script-topic">Topic</label>
          <textarea class="input" id="script-topic" rows="3"
            placeholder="e.g. The lost city buried under the Amazon rainforest"></textarea>
        </div>
        <div class="grid-2">
          <div class="field">
            <label for="script-style">Style</label>
            <select class="input" id="script-style">
              ${SCRIPT_STYLES.map(([v, l]) => `<option value="${v}">${l}</option>`).join("")}
            </select>
          </div>
          <div class="field">
            <label for="script-tone">Tone</label>
            <select class="input" id="script-tone">
              ${SCRIPT_TONES.map(([v, l]) => `<option value="${v}">${l}</option>`).join("")}
            </select>
          </div>
        </div>
        <div class="field">
          <label for="script-length">Target length</label>
          <div class="range-row">
            <input type="range" class="input" id="script-length" min="1" max="30" value="8" />
            <span class="range-val" id="script-length-val">8 min</span>
          </div>
        </div>
        <button class="btn btn-primary" id="script-go" type="submit">Generate script</button>
      </form>
    </div>
    <div id="script-result" style="margin-top:24px"></div>`;

  const lenInput = $("#script-length", main);
  lenInput.addEventListener("input", () => {
    $("#script-length-val", main).textContent = `${lenInput.value} min`;
  });

  $("#script-form", main).addEventListener("submit", async (e) => {
    e.preventDefault();
    const topic = $("#script-topic", main).value.trim();
    if (!topic) { toast("Enter a topic first.", "warning"); return; }
    const style = $("#script-style", main).value;
    const tone = $("#script-tone", main).value;
    const lengthMin = parseInt(lenInput.value, 10);

    const resultEl = $("#script-result", main);
    const btn = $("#script-go", main);
    btn.disabled = true;
    resultEl.innerHTML = jobLoadingHtml("Writing your script…", "This usually takes under a minute.");

    try {
      // Contract: POST /api/jobs/script {"topic","style","tone","length_min"} -> 202 {"job_id"}
      // then poll GET /api/jobs/{id}; result: {"script":{...},"engine":"huggingface|template"}
      const { result } = await runJob(
        "/api/jobs/script",
        { topic, style, tone, length_min: lengthMin },
        (job) => { updateJobLoading(resultEl, job, "Writing your script…"); }
      );
      const script = result.script || {};
      const engine = result.engine || "template";
      renderScriptResult(resultEl, script, { topic, style, tone, lengthMin }, engine);
    } catch (err) {
      resultEl.innerHTML = jobErrorHtml(err.message);
    } finally {
      btn.disabled = false;
    }
  });
}

/** Progress UI shown while a job is pending/running. */
function jobLoadingHtml(title, sub) {
  return `
    <div class="panel loading-block" data-job-loading>
      <div class="spinner"></div>
      <p><strong>${esc(title)}</strong></p>
      <p class="small muted" data-job-status>Starting…</p>
      <div class="progress-bar"><div data-job-bar style="width:8%"></div></div>
      ${sub ? `<p class="small muted">${esc(sub)}</p>` : ""}
    </div>`;
}

function updateJobLoading(container, job, title) {
  const statusEl = $("[data-job-status]", container);
  const barEl = $("[data-job-bar]", container);
  if (statusEl) {
    const label = job.status === "running" ? "Working on it…" : "Queued — starting soon…";
    statusEl.textContent = `${title.replace(/…$/, "")} — ${label}`;
  }
  if (barEl) {
    const pct = job.progress != null ? Math.min(96, Math.max(8, job.progress)) : (job.status === "running" ? 55 : 15);
    barEl.style.width = pct + "%";
  }
}

function jobErrorHtml(msg) {
  return `
    <div class="panel">
      <div class="form-error" style="margin:0">⚠️ ${esc(msg)}</div>
      <p class="small muted" style="margin-top:12px">Nothing was lost — adjust your input and try again.</p>
    </div>`;
}

function renderScriptResult(el, script, meta, engine) {
  const beats = Array.isArray(script.beats) ? script.beats : [];
  const engineLabel = engine === "huggingface" ? "AI" : "Template";
  const plain = scriptToPlainText(script, meta);

  el.innerHTML = `
    <div class="panel script-output">
      <span class="engine-badge">⚡ ${esc(engineLabel)} engine</span>
      <h2>${esc(script.title || meta.topic)}</h2>
      <p class="small muted">${esc(meta.style)} · ${esc(meta.tone)} tone · ~${esc(meta.lengthMin)} min
        ${script.estimated_words ? ` · ≈${esc(script.estimated_words)} words` : ""}</p>
      ${script.hook ? `<div class="script-hook">“${esc(script.hook)}”</div>` : ""}
      ${beats.map((b, i) => `
        <div class="beat">
          <h4>${i + 1}. ${esc(b.heading || "Beat " + (i + 1))}</h4>
          <p class="narration">${esc(b.narration || "")}</p>
          ${b.visual_note ? `<p class="visual">🎬 Visual: ${esc(b.visual_note)}</p>` : ""}
        </div>`).join("")}
      ${script.cta ? `<div class="script-cta"><strong>📣 CTA:</strong> ${esc(script.cta)}</div>` : ""}
      <div class="output-actions">
        <button class="btn btn-ghost btn-sm" id="script-copy">📋 Copy script</button>
        <button class="btn btn-ghost btn-sm" id="script-download">⬇️ Download .txt</button>
        <button class="btn btn-accent btn-sm" id="script-save">💾 Save to My Videos</button>
      </div>
    </div>`;

  $("#script-copy", el).addEventListener("click", async () => {
    const ok = await copyText(plain);
    toast(ok ? "Script copied to clipboard." : "Copy failed — select the text manually.", ok ? "success" : "error");
  });
  $("#script-download", el).addEventListener("click", () => {
    downloadBlob(new Blob([plain], { type: "text/plain" }), slugify(script.title || meta.topic) + ".txt");
  });
  $("#script-save", el).addEventListener("click", async (e) => {
    const btn = e.currentTarget;
    btn.disabled = true;
    try {
      await api("/api/library", {
        method: "POST",
        body: { type: "script", title: script.title || meta.topic, payload: { script, meta, engine } },
      });
      toast("Saved to My Videos.", "success");
    } catch (err) {
      toast("Save failed: " + err.message, "error");
      btn.disabled = false;
    }
  });
}

/** Flatten a script object to plain text for copy/download. */
function scriptToPlainText(script, meta) {
  const lines = [];
  lines.push(script.title || meta.topic || "Untitled script");
  lines.push("=".repeat(40));
  if (script.hook) lines.push("\nHOOK:\n" + script.hook);
  (script.beats || []).forEach((b, i) => {
    lines.push(`\n--- Beat ${i + 1}: ${b.heading || ""} ---`);
    if (b.narration) lines.push(b.narration);
    if (b.visual_note) lines.push("[Visual: " + b.visual_note + "]");
  });
  if (script.cta) lines.push("\nCTA:\n" + script.cta);
  return lines.join("\n");
}

function downloadBlob(blob, filename) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 4000);
}

function slugify(s) {
  return String(s || "untitled").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 60) || "untitled";
}

/* ==========================================================================
   VOICEOVER STUDIO (#/app/voiceover)
   ========================================================================== */

const VOICES = [
  ["aria", "Aria — warm female narrator"],
  ["guy", "Guy — deep male narrator"],
  ["jenny", "Jenny — bright female voice"],
  ["sonia", "Sonia — calm female voice"],
  ["davis", "Davis — steady male storyteller"],
  ["amber", "Amber — energetic female voice"],
];

function renderVoiceoverStudio(main) {
  main.innerHTML = `
    <div class="dash-head">
      <h1>🎙️ Voiceover Studio</h1>
      <p>Paste your script, pick a voice, download studio-quality narration.</p>
    </div>
    <div class="panel">
      <form id="voice-form" novalidate>
        <div class="field">
          <label for="voice-text">Script text</label>
          <textarea class="input" id="voice-text" rows="7" maxlength="3000"
            placeholder="Paste the narration text here (max 3000 characters)…"></textarea>
          <div class="char-count" id="voice-count">0 / 3000</div>
        </div>
        <div class="field">
          <label for="voice-select">Voice</label>
          <select class="input" id="voice-select">
            ${VOICES.map(([v, l]) => `<option value="${v}">${esc(l)}</option>`).join("")}
          </select>
        </div>
        <div class="grid-2">
          <div class="field">
            <label for="voice-rate">Speaking rate</label>
            <div class="range-row">
              <input type="range" class="input" id="voice-rate" min="-50" max="50" value="0" />
              <span class="range-val" id="voice-rate-val">+0%</span>
            </div>
          </div>
          <div class="field">
            <label for="voice-pitch">Pitch</label>
            <div class="range-row">
              <input type="range" class="input" id="voice-pitch" min="-20" max="20" value="0" />
              <span class="range-val" id="voice-pitch-val">+0 Hz</span>
            </div>
          </div>
        </div>
        <button class="btn btn-primary" id="voice-go" type="submit">Generate voiceover</button>
      </form>
    </div>
    <div id="voice-result" style="margin-top:24px"></div>`;

  const textEl = $("#voice-text", main);
  const countEl = $("#voice-count", main);
  textEl.addEventListener("input", () => {
    const n = textEl.value.length;
    countEl.textContent = `${n} / 3000`;
    countEl.classList.toggle("over", n >= 3000);
  });

  const rateEl = $("#voice-rate", main);
  rateEl.addEventListener("input", () => {
    const v = parseInt(rateEl.value, 10);
    $("#voice-rate-val", main).textContent = (v >= 0 ? "+" : "") + v + "%";
  });
  const pitchEl = $("#voice-pitch", main);
  pitchEl.addEventListener("input", () => {
    const v = parseInt(pitchEl.value, 10);
    $("#voice-pitch-val", main).textContent = (v >= 0 ? "+" : "") + v + " Hz";
  });

  $("#voice-form", main).addEventListener("submit", async (e) => {
    e.preventDefault();
    const text = textEl.value.trim();
    if (!text) { toast("Paste some text first.", "warning"); return; }
    if (text.length > 3000) { toast("Text is over the 3000-character limit.", "warning"); return; }
    const voice = $("#voice-select", main).value;
    const rate = parseInt(rateEl.value, 10);   // percent, -50..+50
    const pitch = parseInt(pitchEl.value, 10);  // Hz, -20..+20

    const resultEl = $("#voice-result", main);
    const btn = $("#voice-go", main);
    btn.disabled = true;
    resultEl.innerHTML = jobLoadingHtml("Generating voiceover…", "Rendering audio usually takes under a minute.");

    try {
      // Contract: POST /api/jobs/voiceover {"text","voice","rate","pitch"} -> 202 {"job_id"}
      // then poll; result: {"audio_url":"/api/jobs/{id}/audio"}
      const { jobId, result } = await runJob(
        "/api/jobs/voiceover",
        { text, voice, rate, pitch },
        (job) => updateJobLoading(resultEl, job, "Generating voiceover…")
      );
      const audioUrl = result.audio_url || `/api/jobs/${encodeURIComponent(jobId)}/audio`;
      resultEl.innerHTML = `
        <div class="panel audio-block">
          <h3 style="margin-top:0">🔊 Your voiceover is ready</h3>
          <p class="small muted">${esc(VOICES.find(([v]) => v === voice)?.[1] || voice)} · rate ${(rate >= 0 ? "+" : "") + rate}% · pitch ${(pitch >= 0 ? "+" : "") + pitch} Hz</p>
          <audio controls preload="metadata" src="${esc(audioUrl)}"></audio>
          <div class="output-actions">
            <a class="btn btn-ghost btn-sm" href="${esc(audioUrl)}" download="nextgen-voiceover.mp3">⬇️ Download MP3</a>
            <button class="btn btn-accent btn-sm" id="voice-save">💾 Save to My Videos</button>
          </div>
        </div>`;
      $("#voice-save", resultEl).addEventListener("click", async (ev) => {
        const b = ev.currentTarget;
        b.disabled = true;
        try {
          await api("/api/library", {
            method: "POST",
            body: {
              type: "voiceover",
              title: text.slice(0, 60) + (text.length > 60 ? "…" : ""),
              payload: { text, voice, rate, pitch, audio_url: audioUrl, job_id: jobId },
            },
          });
          toast("Saved to My Videos.", "success");
        } catch (err) {
          toast("Save failed: " + err.message, "error");
          b.disabled = false;
        }
      });
    } catch (err) {
      resultEl.innerHTML = jobErrorHtml(err.message);
    } finally {
      btn.disabled = false;
    }
  });
}

/* ==========================================================================
   THUMBNAIL STUDIO (#/app/thumbnail) — fully client-side 1280×720 canvas
   ========================================================================== */

const THUMB_W = 1280;
const THUMB_H = 720;

/** Background presets — all drawn with canvas gradients, no external images. */
const THUMB_BGS = [
  { name: "Violet Storm", draw: (c, x) => bgBase(c, x, ["#1a0533", "#7c3aed", "#00e5ff"]) },
  { name: "Midnight Ember", draw: (c, x) => bgBase(c, x, ["#0b0603", "#7c2d12", "#f59e0b"]) },
  { name: "Deep Ocean", draw: (c, x) => bgBase(c, x, ["#020617", "#0c4a6e", "#22d3ee"]) },
  { name: "Neon Jungle", draw: (c, x) => bgBase(c, x, ["#02120a", "#14532d", "#4ade80"]) },
  { name: "Crimson Alert", draw: (c, x) => bgBase(c, x, ["#160202", "#7f1d1d", "#f87171"]) },
];

function bgBase(ctx, x, [dark, mid, glow]) {
  // Diagonal dark→mid gradient.
  const g = ctx.createLinearGradient(0, 0, THUMB_W, THUMB_H);
  g.addColorStop(0, dark);
  g.addColorStop(0.55, mid);
  g.addColorStop(1, dark);
  ctx.fillStyle = g;
  ctx.fillRect(0, 0, THUMB_W, THUMB_H);
  // Radial glow, offset per preset index for variety.
  const gx = THUMB_W * (0.25 + 0.12 * (x % 3));
  const gy = THUMB_H * (0.3 + 0.1 * (x % 2));
  const r = ctx.createRadialGradient(gx, gy, 40, gx, gy, 520);
  r.addColorStop(0, glow + "55");
  r.addColorStop(1, "transparent");
  ctx.fillStyle = r;
  ctx.fillRect(0, 0, THUMB_W, THUMB_H);
  // Vignette.
  const v = ctx.createRadialGradient(THUMB_W / 2, THUMB_H / 2, THUMB_H * 0.35, THUMB_W / 2, THUMB_H / 2, THUMB_W * 0.75);
  v.addColorStop(0, "transparent");
  v.addColorStop(1, "rgba(0,0,0,0.55)");
  ctx.fillStyle = v;
  ctx.fillRect(0, 0, THUMB_W, THUMB_H);
}

const THUMB_COLORS = ["#ffffff", "#00e5ff", "#fbbf24", "#f87171", "#34d399", "#a78bfa", "#0a0a0a"];

function renderThumbnailStudio(main) {
  // Local editor state (no backend calls in this tool).
  const state = {
    bg: 0,
    bgImage: null, // AI-generated background (HTMLImageElement) when set
    layers: [
      { text: "YOUR TITLE HERE", size: 120, color: "#ffffff", accent: "#00e5ff", x: 640, y: 300, stroke: true },
    ],
    selected: 0,
  };

  main.innerHTML = `
    <div class="dash-head">
      <h1>🖼️ Thumbnail Studio</h1>
      <p>Design 1280×720 thumbnails in your browser — click the canvas or use sliders to move text.</p>
    </div>
    <div class="panel" style="margin-bottom:20px">
      <h3 style="margin-top:0">🤖 AI thumbnail from your script</h3>

               <p class="small muted">Paste your video script — AI designs a thumbnail concept from the story and renders an HD 16:9 image. Then drop it into the editor below, add the text, and download.</p>
      <div class="field">
        <label for="thumb-script">Your video script</label>
        <textarea class="input" id="thumb-script" rows="5" placeholder="Paste your full video script here…"></textarea>
      </div>
      <button class="btn btn-primary" id="thumb-ai-go" type="button">✨ Generate AI thumbnail</button>
      <div id="thumb-ai-result" style="margin-top:16px"></div>
    </div>
    <div class="thumb-layout">
      <div>
        <div class="thumb-canvas-wrap">
          <canvas id="thumb-canvas" width="${THUMB_W}" height="${THUMB_H}"></canvas>
        </div>
        <div class="output-actions">
          <button class="btn btn-ghost btn-sm" id="thumb-shuffle">🔀 Shuffle style</button>
          <button class="btn btn-primary btn-sm" id="thumb-download">⬇️ Download PNG</button>
        </div>
      </div>
      <div class="thumb-controls">
        <div class="panel" style="padding:18px">
          <div class="field">
            <label>Background</label>
            <div class="bg-presets" id="thumb-bgs"></div>
          </div>
          <div class="field">
            <label>Text layers (max 3)</label>
            <div class="layer-list" id="thumb-layers"></div>
            <button class="btn btn-ghost btn-sm" id="thumb-add-layer" style="width:100%">+ Add text layer</button>
          </div>
          <div id="thumb-layer-editor"></div>
        </div>
      </div>
    </div>`;

  const canvas = $("#thumb-canvas", main);
  const ctx = canvas.getContext("2d");

  /** Redraw the full thumbnail from state. */
  function draw() {
    if (state.bgImage) {
      const img = state.bgImage;
      const scale = Math.max(THUMB_W / img.width, THUMB_H / img.height);
      const w = img.width * scale, h = img.height * scale;
      ctx.drawImage(img, (THUMB_W - w) / 2, (THUMB_H - h) / 2, w, h);
    } else {
      THUMB_BGS[state.bg].draw(ctx, state.bg);
    }
    state.layers.forEach((L) => {
      ctx.save();
      ctx.font = `800 ${L.size}px "Space Grotesk", "Inter", sans-serif`;
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      const lines = L.text.split("\n");
      const lh = L.size * 1.08;
      const y0 = L.y - ((lines.length - 1) * lh) / 2;
      lines.forEach((line, i) => {
        const y = y0 + i * lh;
        if (L.stroke) {
          ctx.lineWidth = Math.max(4, L.size * 0.09);
          ctx.strokeStyle = "rgba(0,0,0,0.85)";
          ctx.strokeText(line, L.x, y);
        }
        // Accent underline glow behind text.
        ctx.fillStyle = L.accent + "33";
        const w = ctx.measureText(line).width;
        ctx.fillRect(L.x - w / 2 - 14, y + L.size * 0.42, w + 28, L.size * 0.12);
        ctx.fillStyle = L.color;
        ctx.fillText(line, L.x, y);
      });
      ctx.restore();
    });
  }

  /** Rebuild the layer list + editor panel. */
  function renderControls() {
    const listEl = $("#thumb-layers", main);
    listEl.innerHTML = "";
    state.layers.forEach((L, i) => {
      const item = document.createElement("div");
      item.className = "layer-item" + (i === state.selected ? " active" : "");
      const label = document.createElement("span");
      label.textContent = `Layer ${i + 1}: ${(L.text.split("\n")[0] || "").slice(0, 18) || "(empty)"}`;
      const del = document.createElement("button");
      del.className = "del";
      del.title = "Delete layer";
      del.textContent = "✕";
      del.addEventListener("click", (e) => {
        e.stopPropagation();
        if (state.layers.length <= 1) { toast("Keep at least one text layer.", "warning"); return; }
        state.layers.splice(i, 1);
        state.selected = Math.max(0, state.selected - 1);
        draw(); renderControls();
      });
      item.appendChild(label);
      item.appendChild(del);
      item.addEventListener("click", () => { state.selected = i; draw(); renderControls(); });
      listEl.appendChild(item);
    });
    $("#thumb-add-layer", main).disabled = state.layers.length >= 3;

    // Editor for the selected layer.
    const L = state.layers[state.selected];
    const ed = $("#thumb-layer-editor", main);
    ed.innerHTML = `
      <div class="field">
        <label>Text (line break = new line)</label>
        <textarea class="input" id="tl-text" rows="2">${esc(L.text)}</textarea>
      </div>
      <div class="field">
        <label>Font size</label>
        <div class="range-row">
          <input type="range" class="input" id="tl-size" min="40" max="220" value="${L.size}" />
          <span class="range-val" id="tl-size-val">${L.size}px</span>
        </div>
      </div>
      <div class="field">
        <label>Text color</label>
        <div class="swatches" id="tl-colors"></div>
      </div>
      <div class="field">
        <label>Accent color</label>
        <div class="swatches" id="tl-accents"></div>
      </div>
      <div class="field">
        <label>Position X</label>
        <div class="range-row">
          <input type="range" class="input" id="tl-x" min="0" max="${THUMB_W}" value="${L.x}" />
          <span class="range-val" id="tl-x-val">${L.x}</span>
        </div>
      </div>
      <div class="field">
        <label>Position Y</label>
        <div class="range-row">
          <input type="range" class="input" id="tl-y" min="0" max="${THUMB_H}" value="${L.y}" />
          <span class="range-val" id="tl-y-val">${L.y}</span>
        </div>
      </div>
      <div class="field">
        <label><input type="checkbox" id="tl-stroke" ${L.stroke ? "checked" : ""} /> Dark outline</label>
      </div>`;

    const sw = (rootId, current, apply) => {
      const root = $(rootId, ed);
      THUMB_COLORS.forEach((c) => {
        const b = document.createElement("button");
        b.className = "swatch" + (c === current ? " active" : "");
        b.style.background = c;
        b.title = c;
        b.addEventListener("click", () => { apply(c); draw(); renderControls(); });
        root.appendChild(b);
      });
    };
    sw("#tl-colors", L.color, (c) => { L.color = c; });
    sw("#tl-accents", L.accent, (c) => { L.accent = c; });

    $("#tl-text", ed).addEventListener("input", (e) => { L.text = e.target.value; draw(); });
    $("#tl-size", ed).addEventListener("input", (e) => {
      L.size = parseInt(e.target.value, 10);
      $("#tl-size-val", ed).textContent = L.size + "px";
      draw();
    });
    const bindXY = (id, valId, key) => {
      $(id, ed).addEventListener("input", (e) => {
        L[key] = parseInt(e.target.value, 10);
        $(valId, ed).textContent = L[key];
        draw();
      });
    };
    bindXY("#tl-x", "#tl-x-val", "x");
    bindXY("#tl-y", "#tl-y-val", "y");
    $("#tl-stroke", ed).addEventListener("change", (e) => { L.stroke = e.target.checked; draw(); });
  }

  // Background preset buttons (mini gradient swatches).
  const bgRoot = $("#thumb-bgs", main);
  THUMB_BGS.forEach((bg, i) => {
    const b = document.createElement("button");
    b.className = "bg-preset" + (i === 0 ? " active" : "");
    b.title = bg.name;
    b.style.background = `linear-gradient(135deg, #0a0f1e, #7c3aed)`;
    // Paint a tiny preview using the same draw routine on an offscreen canvas.
    const prev = document.createElement("canvas");
    prev.width = 96; prev.height = 54;
    const pctx = prev.getContext("2d");
    pctx.save();
    pctx.scale(96 / THUMB_W, 54 / THUMB_H);
    bg.draw(pctx, i);
    pctx.restore();
    b.style.background = `url(${prev.toDataURL()})`;
    b.style.backgroundSize = "cover";
    b.addEventListener("click", () => {
      state.bg = i;
      state.bgImage = null;
      $$(".bg-preset", bgRoot).forEach((el, j) => el.classList.toggle("active", j === i));
      draw();
    });
    bgRoot.appendChild(b);
  });

  $("#thumb-add-layer", main).addEventListener("click", () => {
    if (state.layers.length >= 3) { toast("Maximum 3 text layers.", "warning"); return; }
    state.layers.push({ text: "NEW TEXT", size: 90, color: "#ffffff", accent: "#fbbf24", x: 640, y: 520, stroke: true });
    state.selected = state.layers.length - 1;
    draw(); renderControls();
  });

  // Drag text layers directly on the canvas.
  let dragIdx = -1;
  const canvasPos = (e) => {
    const r = canvas.getBoundingClientRect();
    return {
      x: Math.round((e.clientX - r.left) * (THUMB_W / r.width)),
      y: Math.round((e.clientY - r.top) * (THUMB_H / r.height)),
    };
  };
  canvas.addEventListener("pointerdown", (e) => {
    const p = canvasPos(e);
    // Hit-test from top layer down (rough box around layer anchor).
    for (let i = state.layers.length - 1; i >= 0; i--) {
      const L = state.layers[i];
      if (Math.abs(p.x - L.x) < 320 && Math.abs(p.y - L.y) < L.size) {
        dragIdx = i;
        state.selected = i;
        canvas.setPointerCapture(e.pointerId);
        renderControls();
        break;
      }
    }
  });
  canvas.addEventListener("pointermove", (e) => {
    if (dragIdx < 0) return;
    const p = canvasPos(e);
    const L = state.layers[dragIdx];
    L.x = Math.max(0, Math.min(THUMB_W, p.x));
    L.y = Math.max(0, Math.min(THUMB_H, p.y));
    draw();
  });
  canvas.addEventListener("pointerup", () => { dragIdx = -1; renderControls(); });

  $("#thumb-shuffle", main).addEventListener("click", () => {
    // Randomize background, colors and sizes for a fresh look.
    state.bg = Math.floor(Math.random() * THUMB_BGS.length);
    state.bgImage = null;
    const pick = (arr) => arr[Math.floor(Math.random() * arr.length)];
    state.layers.forEach((L) => {
      L.color = pick(THUMB_COLORS.filter((c) => c !== "#0a0a0a"));
      L.accent = pick(THUMB_COLORS);
      L.size = 80 + Math.floor(Math.random() * 70);
    });
    $$(".bg-preset", bgRoot).forEach((el, j) => el.classList.toggle("active", j === state.bg));
    draw(); renderControls();
    toast("Style shuffled 🎲", "success");
  });

  $("#thumb-download", main).addEventListener("click", () => {
    canvas.toBlob((blob) => {
      if (!blob) { toast("Export failed in this browser.", "error"); return; }
      downloadBlob(blob, "nextgen-thumbnail-1280x720.png");
      toast("Thumbnail downloaded.", "success");
    }, "image/png");
  });

  // --- AI thumbnail from script -------------------------------------------
  $("#thumb-ai-go", main).addEventListener("click", async () => {
    const script = $("#thumb-script", main).value.trim();
    if (script.length < 30) { toast("Paste your video script first (at least a short paragraph).", "warning"); return; }
    const btn = $("#thumb-ai-go", main);
    const out = $("#thumb-ai-result", main);
    btn.disabled = true;
    out.innerHTML = jobLoadingHtml("Generating your thumbnail…", "Reading the script, designing the concept and rendering the HD image — usually under a minute.");
    try {
      const { result } = await runJob(
        "/api/jobs/thumbnail-ai",
        { script },
        (job) => {
          const s = $("[data-job-status]", out);
          if (s) s.textContent = job.progress || (job.status === "running" ? "Working on it…" : "Queued — starting soon…");
          const b = $("[data-job-bar]", out);
          if (b) b.style.width = job.status === "running" ? "60%" : "15%";
        }
      );
      const r = result || {};
      out.innerHTML = `
        <img src="${r.image_data_url}" alt="AI thumbnail" style="width:100%;max-width:640px;border-radius:10px;display:block" />
        ${r.thumbnail_text ? `<p style="margin:10px 0 4px">Suggested on-image text: <strong>${esc(r.thumbnail_text)}</strong></p>` : ""}
        <div class="output-actions">
          <button class="btn btn-accent btn-sm" id="thumb-ai-use" type="button">🎨 Use in editor (background + text)</button>
          <a class="btn btn-ghost btn-sm" href="${r.image_data_url}" download="ai-thumbnail.png">⬇️ Download image</a>
        </div>`;
      $("#thumb-ai-use", out).addEventListener("click", () => {
        const img = new Image();
        img.onload = () => {
          state.bgImage = img;
          if (r.thumbnail_text) {
            state.layers[0].text = r.thumbnail_text.toUpperCase();
            state.selected = 0;
          }
          $$(".bg-preset", bgRoot).forEach((el) => el.classList.remove("active"));
          draw(); renderControls();
          toast("AI thumbnail loaded into the editor.", "success");
        };
        img.src = r.image_data_url;
      });
    } catch (err) {
      out.innerHTML = jobErrorHtml(err.message);
    } finally {
      btn.disabled = false;
    }
  });

  draw();
  renderControls();
}

/* ==========================================================================
   NICHE SCREENER (#/app/niches) — GET /api/niches, searchable/sortable cards
   ========================================================================== */

function renderNicheScreener(main) {
  main.innerHTML = `
    <div class="dash-head">
      <h1>🎯 Niche Screener</h1>
      <p>Scored faceless niches — search, sort, and expand for video ideas.</p>
    </div>
    <div class="toolbar">
      <input class="input" id="niche-search" type="search" placeholder="🔍 Search niches…" />
      <select class="input" id="niche-sort" style="max-width:240px">
        <option value="name">Sort: Name (A–Z)</option>
        <option value="competition">Sort: Competition (low → high)</option>
        <option value="difficulty">Sort: Difficulty (easy → hard)</option>
      </select>
    </div>
    <div id="niche-body"></div>`;

  const body = $("#niche-body", main);
  const searchEl = $("#niche-search", main);
  const sortEl = $("#niche-sort", main);
  let niches = [];
  let expanded = null;

  /** Normalize one niche record — tolerates a few likely field-name variants. */
  const norm = (n) => ({
    name: n.name || n.title || n.niche || "Untitled niche",
    competition: n.competition || n.competition_level || "—",
    difficulty: n.difficulty || n.difficulty_level || "—",
    why: n.why_it_works || n.whyItWorks || n.description || "",
    cpm: n.cpm_note || n.cpmNote || n.cpm || "",
    ideas: n.video_ideas || n.videoIdeas || n.ideas || [],
  });

  /** Numeric rank for competition/difficulty sorting (low=easy first). */
  const rank = (v) => {
    const s = String(v || "").toLowerCase();
    if (/^(low|easy|1)/.test(s)) return 0;
    if (/^(medium|moderate|2)/.test(s)) return 1;
    if (/^(high|hard|3)/.test(s)) return 2;
    const num = parseFloat(s);
    return Number.isFinite(num) ? num : 9;
  };

  const tagClass = (v) => {
    const r = rank(v);
    return r === 0 ? "tag low" : r === 1 ? "tag med" : r === 2 ? "tag high" : "tag";
  };

  function paint() {
    const q = searchEl.value.trim().toLowerCase();
    const sort = sortEl.value;
    let list = niches.map(norm).filter((n) =>
      !q || n.name.toLowerCase().includes(q) || n.why.toLowerCase().includes(q));

    list.sort((a, b) => {
      if (sort === "competition") return rank(a.competition) - rank(b.competition);
      if (sort === "difficulty") return rank(a.difficulty) - rank(b.difficulty);
      return a.name.localeCompare(b.name);
    });

    if (!list.length) {
      body.innerHTML = `<div class="panel center muted">No niches match your search.</div>`;
      return;
    }

    body.innerHTML = `<div class="niche-grid">${list.map((n) => {
      const id = "n-" + n.name.replace(/[^a-z0-9]/gi, "").toLowerCase();
      const open = expanded === n.name;
      return `
      <div class="card niche-card" data-niche="${esc(n.name)}">
        <h3>${esc(n.name)}</h3>
        <div class="niche-meta">
          <span class="${tagClass(n.competition)}">Competition: ${esc(n.competition)}</span>
          <span class="${tagClass(n.difficulty)}">Difficulty: ${esc(n.difficulty)}</span>
        </div>
        ${open ? `
        <div class="niche-detail">
          ${n.why ? `<p><strong>Why it works:</strong> ${esc(n.why)}</p>` : ""}
          ${n.cpm ? `<p><strong>💰 CPM note:</strong> ${esc(n.cpm)}</p>` : ""}
          ${n.ideas.length ? `<p><strong>🎬 Video ideas:</strong></p><ul>${n.ideas.map((i) => `<li>${esc(i)}</li>`).join("")}</ul>` : ""}
        </div>` : `<p class="small muted">Click to expand — why it works, CPM note & video ideas.</p>`}
      </div>`;
    }).join("")}</div>`;

    $$(".niche-card", body).forEach((card) => {
      card.addEventListener("click", () => {
        expanded = expanded === card.dataset.niche ? null : card.dataset.niche;
        paint();
      });
    });
  }

  body.innerHTML = `<div class="loading-block"><div class="spinner"></div><p>Loading niches…</p></div>`;
  // Contract: GET /api/niches -> {"niches":[...]}
  api("/api/niches")
    .then((data) => {
      niches = (data && data.niches) || [];
      paint();
    })
    .catch((err) => {
      body.innerHTML = jobErrorHtml("Could not load niches: " + err.message);
    });

  searchEl.addEventListener("input", paint);
  sortEl.addEventListener("change", paint);
}

/* ==========================================================================
   CHANNEL ANALYZER (#/app/analyzer)
   ========================================================================== */

function renderChannelAnalyzer(main) {
  main.innerHTML = `
    <div class="dash-head">
      <h1>📊 Channel Analyzer</h1>
      <p>Paste any YouTube channel URL — get stats, top videos and posting insights.</p>
    </div>
    <div class="panel">
      <form id="analyzer-form" novalidate>
        <div class="field">
          <label for="analyzer-url">YouTube channel URL</label>
          <input class="input" id="analyzer-url" type="url"
            placeholder="https://www.youtube.com/@somechannel" />
          <div class="hint">Channel page, @handle, /c/ or /channel/ URLs all work.</div>
        </div>
        <div class="field">
          <label for="analyzer-max">Videos to analyze</label>
          <div class="range-row">
            <input type="range" class="input" id="analyzer-max" min="5" max="50" value="20" />
            <span class="range-val" id="analyzer-max-val">20</span>
          </div>
        </div>
        <button class="btn btn-primary" id="analyzer-go" type="submit">Analyze channel</button>
        <div class="field" style="margin-top:20px;border-top:1px solid var(--border,#2a2a3a);padding-top:16px">
          <label>🔍 Full analysis — research JSON</label>
          <div class="hint">Last 50–100 videos with <strong>views + upload dates</strong>, outliers vs the channel median,
            transcripts of the top 5 and bottom 3 videos, top 30 comments on the best videos and
            audience requests for a next film — all in one downloadable JSON. Takes a few minutes.</div>
          <div class="range-row" style="margin-top:8px">
            <select class="input" id="analyzer-full-count" style="max-width:150px">
              <option value="50" selected>50 videos</option>
              <option value="100">100 videos</option>
            </select>
            <button class="btn btn-accent" id="analyzer-full-go" type="button">Run full analysis</button>
          </div>
        </div>
      </form>
    </div>
    <div id="analyzer-result" style="margin-top:24px"></div>`;
  const maxEl = $("#analyzer-max", main);
  maxEl.addEventListener("input", () => {
    $("#analyzer-max-val", main).textContent = maxEl.value;
  });

  $("#analyzer-form", main).addEventListener("submit", async (e) => {
    e.preventDefault();
    const url = $("#analyzer-url", main).value.trim();
    if (!url) { toast("Paste a YouTube channel URL first.", "warning"); return; }
    if (!/^(https?:\/\/)?(www\.|m\.)?(youtube\.com|youtu\.be)\//i.test(url)) {
      toast("That doesn't look like a YouTube URL.", "warning");
      return;
    }
    const maxVideos = parseInt(maxEl.value, 10);

    const resultEl = $("#analyzer-result", main);
    const btn = $("#analyzer-go", main);
    btn.disabled = true;
    resultEl.innerHTML = jobLoadingHtml("Analyzing channel…", "Fetching videos and computing stats — can take a couple of minutes for large channels.");

    try {
      // Contract: POST /api/jobs/analyze {"youtube_url","max_videos"} -> 202 {"job_id"}
      // then poll GET /api/jobs/{id}; result has channel/videos/insights.
      const { jobId, result } = await runJob(
        "/api/jobs/analyze",
        { youtube_url: url, max_videos: maxVideos },
        (job) => updateJobLoading(resultEl, job, "Analyzing channel…")
      );
      renderAnalysisResult(resultEl, result || {}, jobId, url);
    } catch (err) {
      resultEl.innerHTML = jobErrorHtml(friendlyAnalyzeError(err.message));
    } finally {
      btn.disabled = false;
    }
  });

  $("#analyzer-full-go", main).addEventListener("click", async () => {
    const url = $("#analyzer-url", main).value.trim();
    if (!url) { toast("Paste a YouTube channel URL first.", "warning"); return; }
    if (!/^(https?:\/\/)?(www\.|m\.)?(youtube\.com|youtu\.be)\//i.test(url)) {
      toast("That doesn't look like a YouTube URL.", "warning");
      return;
    }
    const count = parseInt($("#analyzer-full-count", main).value, 10) || 50;
    const resultEl = $("#analyzer-result", main);
    const fbtn = $("#analyzer-full-go", main);
    fbtn.disabled = true;
    resultEl.innerHTML = jobLoadingHtml("Running full analysis…",
      "Fetching every video's views and dates, then transcripts and comments. This can take several minutes — keep this tab open.");
    try {
      const { jobId, result } = await runJob(
        "/api/jobs/analyze-full",
        { youtube_url: url, max_videos: count },
        (job) => {
          const s = $("[data-job-status]", resultEl);
          if (s) s.textContent = job.progress || (job.status === "running" ? "Working on it…" : "Queued — starting soon…");
          const b = $("[data-job-bar]", resultEl);
          if (b) b.style.width = job.status === "running" ? "60%" : "15%";
        }
      );
      renderFullResult(resultEl, result || {}, jobId);
    } catch (err) {
      resultEl.innerHTML = jobErrorHtml(friendlyAnalyzeError(err.message));
    } finally {
      fbtn.disabled = false;
    }
  });
}

/** ISO-8601 duration (PT1H2M3S) -> display text (1:02:03). */
function isoDurText(iso) {
  if (!iso || typeof iso !== "string") return "—";
  const m = iso.match(/PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?/);
  if (!m) return iso;
  const h = +(m[1] || 0), mi = +(m[2] || 0), s = +(m[3] || 0);
  return h ? `${h}:${String(mi).padStart(2, "0")}:${String(s).padStart(2, "0")}`
           : `${mi}:${String(s).padStart(2, "0")}`;
}

function renderFullResult(el, result, jobId) {
  const md = result.metadata || {};
  const videos = Array.isArray(result.videos) ? result.videos : [];
  const outliers = Array.isArray(result.outliers) ? result.outliers : [];
  const transcripts = Array.isArray(result.transcripts) ? result.transcripts : [];
  const tstat = result.transcript_status || {};
  const topComments = Array.isArray(result.top_comments) ? result.top_comments : [];
  const requests = Array.isArray(result.next_film_requests) ? result.next_film_requests : [];
  const name = md.channel_name || "Channel";
  const firstLetter = String(name).trim().charAt(0).toUpperCase() || "?";

  el.innerHTML = `
    <div class="panel">
      <div class="channel-header">
        <div class="channel-avatar">${esc(firstLetter)}</div>
        <div>
          <h2>${esc(name)}</h2>
          <p class="handle">${md.channel_id ? esc(md.channel_id) : ""}</p>
          <p class="handle">${md.subscribers != null ? `<strong style="color:var(--text)">${esc(fmtNum(md.subscribers))}</strong> subscribers` : ""}</p>
        </div>
      </div>
      <div class="stat-row">
        <div class="card stat"><div class="val">${esc(fmtNum(videos.length))}</div><div class="lbl">Videos fetched</div></div>
        <div class="card stat"><div class="val">${esc(fmtNum(md.video_count))}</div><div class="lbl">Videos on channel</div></div>
        <div class="card stat"><div class="val">${esc(fmtNum(md.total_views))}</div><div class="lbl">Total views (fetched)</div></div>
        <div class="card stat"><div class="val">${esc(fmtNum(result.median_views))}</div><div class="lbl">Median views</div></div>
      </div>

      <h3>🚀 Outliers — far above the channel median</h3>
      <div class="table-wrap"><table class="data">
        <thead><tr><th>Title</th><th>Views</th><th>× median</th><th>Date</th><th></th></tr></thead>
        <tbody>
          ${outliers.map((v) => `<tr>
            <td>${esc(v.title)}</td>
            <td class="num">${esc(fmtNum(v.views))}</td>
            <td class="num">${esc(v.multiple_of_median)}×</td>
            <td class="num">${esc(fmtDate(v.published_at))}</td>
            <td><a href="https://www.youtube.com/watch?v=${esc(v.video_id)}" target="_blank" rel="noopener">Open ↗</a></td>
          </tr>`).join("") || `<tr><td colspan="5" class="muted center">No outliers found in this sample.</td></tr>`}
        </tbody></table></div>

      <h3>🎬 All videos — views & dates</h3>
      <div class="table-wrap"><table class="data">
        <thead><tr><th>Title</th><th>Views</th><th>Likes</th><th>Comments</th><th>Duration</th><th>Date</th><th></th></tr></thead>
        <tbody>
          ${videos.map((v) => `<tr>
            <td>${esc(v.title)}</td>
            <td class="num">${esc(fmtNum(v.views))}</td>
            <td class="num">${esc(fmtNum(v.likes))}</td>
            <td class="num">${esc(fmtNum(v.comments))}</td>
            <td class="num">${esc(isoDurText(v.duration))}</td>
            <td class="num">${esc(fmtDate(v.published_at))}</td>
            <td><a href="https://www.youtube.com/watch?v=${esc(v.video_id)}" target="_blank" rel="noopener">Open ↗</a></td>
          </tr>`).join("") || `<tr><td colspan="7" class="muted center">No videos returned.</td></tr>`}
        </tbody></table></div>

      <h3>📝 Transcripts — top 5 & bottom 3 videos ${tstat.requested != null ? `<span class="muted">(${esc(tstat.fetched ?? transcripts.length)}/${esc(tstat.requested)} fetched)</span>` : ""}</h3>
      ${tstat.warning ? `<p class="small muted">${esc(tstat.warning)}</p>` : ""}
      ${transcripts.map((t) => `
        <details style="margin:8px 0">
          <summary style="cursor:pointer">${esc(t.title)}</summary>
          <div class="small" style="white-space:pre-wrap;max-height:300px;overflow:auto;padding:10px;border:1px solid var(--border,#2a2a3a);border-radius:8px;margin-top:6px">${esc(t.text)}</div>
        </details>`).join("") || `<p class="muted">No transcripts available.</p>`}

      <h3>🎬 Audience requests — asking for a next film (${requests.length})</h3>
      ${requests.map((r) => `
        <div class="res-item"><span>“${esc(r.text)}” <span class="muted">— ${esc(r.author)} on “${esc(r.video_title)}” (♥ ${esc(fmtNum(r.likes))})</span></span></div>
      `).join("") || `<p class="muted">No next-film requests found in the top comments.</p>`}

      <h3>💬 Top comments on the best videos</h3>
      ${result.comments_note ? `<p class="small muted">${esc(result.comments_note)}</p>` : ""}
      ${topComments.map((g) => `
        <details style="margin:8px 0">
          <summary style="cursor:pointer">${esc(g.title)} <span class="muted">(${g.comments.length} comments)</span></summary>
          ${g.comments.map((c) => `
            <div class="res-item" style="margin-top:6px"><span>${c.is_next_film_request ? "🎬 " : ""}“${esc(c.text)}” <span class="muted">— ${esc(c.author)} (♥ ${esc(fmtNum(c.likes))})</span></span></div>
          `).join("")}
        </details>`).join("") || `<p class="muted">No comments fetched.</p>`}

      <div class="output-actions">
        <a class="btn btn-ghost btn-sm" href="/api/jobs/${encodeURIComponent(jobId)}/download" download>⬇️ Download JSON</a>
        <button class="btn btn-ghost btn-sm" id="full-copy">📋 Copy JSON</button>
      </div>
    </div>`;

  $("#full-copy", el).addEventListener("click", async () => {
    const ok = await copyText(JSON.stringify(result, null, 2));
    toast(ok ? "Full analysis JSON copied." : "Copy failed.", ok ? "success" : "error");
  });
}

/** Map raw/technical failures to friendly messages for bad URLs & private channels. */
function friendlyAnalyzeError(msg) {
  const m = String(msg || "").toLowerCase();
  if (m.includes("private") || m.includes("unavailable") || m.includes("not found") || m.includes("404")) {
    return "Could not access that channel — it may be private, deleted, or the URL may be wrong. Double-check the link and try again.";
  }
  if (m.includes("timeout") || m.includes("timed out")) {
    return "The analysis timed out. Try again with fewer videos.";
  }
  return msg;
}

function renderAnalysisResult(el, result, jobId, sourceUrl) {
  // Tolerate a few result shapes: flat or nested under channel/stats/videos/insights.
  const ch = result.channel || {};
  const stats = result.stats || result || {};
  const videos = Array.isArray(result.videos) ? result.videos : [];
  const insights = result.insights || {};

  const name = ch.name || ch.title || result.channel_name || "Channel";
  const handle = ch.handle || ch.custom_url || result.handle || "";
  const subs = ch.subscribers ?? ch.subscriber_count ?? result.subscribers;
  const videoCount = stats.video_count ?? stats.total_videos ?? videos.length;
  const avgViews = stats.avg_views ?? stats.average_views;
  const topVideo = stats.top_video || videos[0] || {};

  const cadence = insights.cadence || insights.cadence_note || insights.upload_cadence || "";
  const notes = Array.isArray(insights.notes) ? insights.notes : [];

  const firstLetter = String(name).trim().charAt(0).toUpperCase() || "?";

  el.innerHTML = `
    <div class="panel">
      <div class="channel-header">
        <div class="channel-avatar">${esc(firstLetter)}</div>
        <div>
          <h2>${esc(name)}</h2>
          ${handle ? `<p class="handle">${esc(handle)}</p>` : ""}
          <p class="handle">${subs != null ? `<strong style="color:var(--text)">${esc(fmtNum(subs))}</strong> subscribers` : ""}</p>
        </div>
      </div>
      <div class="stat-row">
        <div class="card stat"><div class="val">${esc(fmtNum(videoCount))}</div><div class="lbl">Videos analyzed</div></div>
        <div class="card stat"><div class="val">${esc(fmtNum(avgViews))}</div><div class="lbl">Avg views</div></div>
        <div class="card stat"><div class="val">${esc(fmtNum(topVideo.views ?? topVideo.view_count))}</div><div class="lbl">Top video views</div></div>
      </div>
      ${cadence || notes.length ? `
      <div class="insight-box">
        <strong>💡 Insights</strong><br/>
        ${cadence ? esc(cadence) : ""}
        ${notes.map((n) => `<div>• ${esc(n)}</div>`).join("")}
      </div>` : ""}
      <h3>Top videos</h3>
      <div class="table-wrap">
        <table class="data">
          <thead><tr><th>Title</th><th>Views</th><th>Duration</th><th>Date</th><th></th></tr></thead>
          <tbody>
            ${videos.map((v) => {
              const title = v.title || "(untitled)";
              const link = v.url || v.link || v.watch_url || "";
              return `<tr>
                <td>${esc(title)}</td>
                <td class="num">${esc(fmtNum(v.views ?? v.view_count))}</td>
                <td class="num">${esc(fmtDur(v.duration))}</td>
                <td class="num">${esc(fmtDate(v.date || v.published_at || v.published))}</td>
                <td>${link ? `<a href="${esc(link)}" target="_blank" rel="noopener">Open ↗</a>` : ""}</td>
              </tr>`;
            }).join("") || `<tr><td colspan="5" class="muted center">No videos returned.</td></tr>`}
          </tbody>
        </table>
      </div>
      <div class="output-actions">
        <a class="btn btn-ghost btn-sm" href="/api/jobs/${encodeURIComponent(jobId)}/download" download>⬇️ Download JSON</a>
        <button class="btn btn-ghost btn-sm" id="analysis-copy">📋 Copy JSON</button>
        <button class="btn btn-accent btn-sm" id="analysis-save">💾 Save to My Videos</button>
      </div>
    </div>`;

  $("#analysis-copy", el).addEventListener("click", async () => {
    const ok = await copyText(JSON.stringify(result, null, 2));
    toast(ok ? "Analysis JSON copied." : "Copy failed.", ok ? "success" : "error");
  });
  $("#analysis-save", el).addEventListener("click", async (e) => {
    const btn = e.currentTarget;
    btn.disabled = true;
    try {
      await api("/api/library", {
        method: "POST",
        body: { type: "analysis", title: `${name} — channel analysis`, payload: { result, source_url: sourceUrl } },
      });
      toast("Saved to My Videos.", "success");
    } catch (err) {
      toast("Save failed: " + err.message, "error");
      btn.disabled = false;
    }
  });
}

/* ==========================================================================
   RESOURCES (#/app/resources) — GET /api/resources, copy buttons everywhere
   ========================================================================== */

function renderResources(main) {
  main.innerHTML = `
    <div class="dash-head">
      <h1>📚 Resources</h1>
      <p>Proven formulas, checklists and prompts — click copy and use them anywhere.</p>
    </div>
    <div id="res-body"><div class="loading-block"><div class="spinner"></div><p>Loading resources…</p></div></div>`;

  const body = $("#res-body", main);

  /** Extract display text from an item (string or {formula/title, example/text} object). */
  const itemText = (item) => {
    if (typeof item === "string") return item;
    if (item && typeof item === "object") {
      const head = item.formula || item.title || item.name || "";
      const body = item.example || item.text || item.prompt || item.item || "";
      if (head && body) return head + " — " + body;
      return head || body;
    }
    return String(item ?? "");
  };

  const section = (title, icon, items) => {
    if (!items || !items.length) return "";
    return `
      <div class="res-section">
        <h2>${icon} ${esc(title)}</h2>
        ${items.map((it) => {
          const text = itemText(it);
          return `
          <div class="res-item">
            <span>${esc(text)}</span>
            <button class="btn btn-ghost btn-sm copy-btn" data-copy="${esc(text)}">📋 Copy</button>
          </div>`;
        }).join("")}
      </div>`;
  };

  // Contract: GET /api/resources -> {"hooks","titles","checklists","prompts"}
  api("/api/resources")
    .then((data) => {
      const d = data || {};
      const ck = d.checklists || {};
      body.innerHTML =
        section("Hook formulas", "🪝", d.hooks) +
        section("Title formulas", "✍️", d.titles) +
        (Array.isArray(ck)
          ? section("Checklists", "✅", ck)
          : section("Pre-upload checklist", "✅", ck.pre_upload || []) +
            section("Production checklist", "🎬", ck.production || [])) +
        section("Starter prompts", "💡", d.prompts);
      if (!body.innerHTML.trim()) {
        body.innerHTML = `<div class="panel center muted">No resources available yet.</div>`;
      }
      $$("[data-copy]", body).forEach((btn) => {
        btn.addEventListener("click", async () => {
          const ok = await copyText(btn.dataset.copy);
          toast(ok ? "Copied to clipboard." : "Copy failed.", ok ? "success" : "error");
        });
      });
    })
    .catch((err) => {
      body.innerHTML = jobErrorHtml("Could not load resources: " + err.message);
    });
}

/* ==========================================================================
   MY VIDEOS (#/app/videos) — library list / view / delete
   ========================================================================== */

function renderLibrary(main) {
  main.innerHTML = `
    <div class="dash-head">
      <h1>💾 My Videos</h1>
      <p>Your saved scripts and channel analyses.</p>
    </div>
    <div id="lib-body"><div class="loading-block"><div class="spinner"></div><p>Loading your library…</p></div></div>`;

  const body = $("#lib-body", main);
  let openId = null;

  function paintList(items) {
    if (!items.length) {
      body.innerHTML = `
        <div class="panel center">
          <p class="muted">Nothing saved yet.</p>
          <p class="small muted">Generate a script or run a channel analysis, then hit <strong>“Save to My Videos”</strong>.</p>
          <a class="btn btn-primary btn-sm" href="#/app/script">Open Script Studio</a>
        </div>`;
      return;
    }
    body.innerHTML = items.map((it) => {
      const typeLabel = it.type === "script" ? "📝 Script" : it.type === "analysis" ? "📊 Analysis" : it.type === "voiceover" ? "🎙️ Voiceover" : esc(it.type || "Item");
      const isOpen = openId === it.id;
      return `
      <div class="lib-item" data-lib="${esc(it.id)}">
        <div style="min-width:0">
          <p class="t">${esc(it.title || "Untitled")}</p>
          <p class="m">${typeLabel} · ${esc(fmtDate(it.created_at))}</p>
          <div class="lib-detail" data-detail style="${isOpen ? "" : "display:none"}"></div>
        </div>
        <div class="actions">
          <button class="btn btn-ghost btn-sm" data-act="view">${isOpen ? "Hide" : "View"}</button>
          <button class="btn btn-danger-ghost btn-sm" data-act="delete">Delete</button>
        </div>
      </div>`;
    }).join("");

    $$(".lib-item", body).forEach((row) => {
      const id = row.dataset.lib;
      $("[data-act='view']", row).addEventListener("click", async (e) => {
        const btn = e.currentTarget;
        if (openId === id) { openId = null; paintList(items); return; }
        openId = id;
        paintList(items);
        const detail = $(`[data-lib="${CSS.escape(id)}"] [data-detail]`, body);
        detail.style.display = "";
        detail.innerHTML = `<div class="loading-block"><div class="spinner sm"></div><p class="small">Loading…</p></div>`;
        try {
          // Contract: GET /api/library/{id} -> {"item":{...full with payload}}
          const data = await api(`/api/library/${encodeURIComponent(id)}`);
          renderLibraryDetail(detail, (data && data.item) || {});
        } catch (err) {
          detail.innerHTML = `<div class="form-error">⚠️ ${esc(err.message)}</div>`;
        }
      });
      $("[data-act='delete']", row).addEventListener("click", async () => {
        if (!confirm("Delete this saved item? This cannot be undone.")) return;
        try {
          await api(`/api/library/${encodeURIComponent(id)}`, { method: "DELETE" });
          toast("Deleted.", "success");
          if (openId === id) openId = null;
          load();
        } catch (err) {
          toast("Delete failed: " + err.message, "error");
        }
      });
    });
  }

  /** Render an expanded library item: scripts get the rich script view. */
  function renderLibraryDetail(detail, item) {
    const p = item.payload || {};
    if (item.type === "script" && p.script) {
      detail.innerHTML = `<div class="script-output" data-script-detail></div>`;
      const holder = $("[data-script-detail]", detail);
      // Reuse the script renderer (read-only actions hidden by not wiring save).
      const meta = p.meta || {};
      const engine = p.engine || "template";
      renderScriptResult(holder, p.script, meta, engine);
      // Hide the "Save to My Videos" button since it's already saved.
      const saveBtn = $("#script-save", holder);
      if (saveBtn) saveBtn.remove();
      return;
    }
    if (item.type === "analysis" && p.result) {
      detail.innerHTML = `<div data-analysis-detail></div>`;
      renderAnalysisResult($("[data-analysis-detail]", detail), p.result, p.job_id || "saved", p.source_url || "");
      const saveBtn = $("#analysis-save", detail);
      if (saveBtn) saveBtn.remove();
      return;
    }
    if (item.type === "voiceover" && p.text) {
      detail.innerHTML = `
        <p><strong>Voice:</strong> ${esc(p.voice || "—")} · <strong>Rate:</strong> ${(p.rate >= 0 ? "+" : "") + (p.rate ?? 0)}% · <strong>Pitch:</strong> ${(p.pitch >= 0 ? "+" : "") + (p.pitch ?? 0)} Hz</p>
        ${p.audio_url ? `<audio controls preload="metadata" src="${esc(p.audio_url)}" style="width:100%"></audio>
        <div class="output-actions"><a class="btn btn-ghost btn-sm" href="${esc(p.audio_url)}" download="nextgen-voiceover.mp3">⬇️ Download MP3</a></div>` : ""}
        <div class="beat"><p class="narration">${esc(p.text)}</p></div>`;
      return;
    }
    // Generic fallback: pretty-printed payload.
    detail.innerHTML = `<pre class="small muted" style="white-space:pre-wrap;word-break:break-word">${esc(JSON.stringify(p, null, 2))}</pre>`;
  }

  function load() {
    body.innerHTML = `<div class="loading-block"><div class="spinner"></div><p>Loading your library…</p></div>`;
    // Contract: GET /api/library -> {"items":[{"id","type","title","created_at"}]}
    api("/api/library")
      .then((data) => paintList((data && data.items) || []))
      .catch((err) => { body.innerHTML = jobErrorHtml("Could not load your library: " + err.message); });
  }

  load();
}

/* ==========================================================================
   BOOT
   ========================================================================== */

(function init() {
  // Render the initial route, then keep the router in sync with the hash.
  route();
})();
