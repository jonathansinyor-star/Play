import os
import re
import json
import subprocess
import tempfile
import math
from datetime import datetime
from urllib.parse import unquote
from flask import Flask, request, session, Response, send_file, redirect, url_for
import requests as req
from groq import Groq

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-me-in-railway")

# ---------------------------------------------------------------------------
# Ensure Playwright Chromium is installed (build-time install may be skipped
# by Railway's layer cache; this catches that case at first startup).
# Runs in a background thread so it never blocks app startup.
# ---------------------------------------------------------------------------
def _ensure_chromium():
    pw_cache = os.path.join(os.path.expanduser("~"), ".cache", "ms-playwright")
    try:
        already = os.path.isdir(pw_cache) and any(
            "chromium" in d for d in os.listdir(pw_cache)
        )
    except Exception:
        already = False
    if already:
        return
    try:
        subprocess.run(
            ["playwright", "install", "chromium", "--with-deps"],
            timeout=180,
        )
    except Exception:
        try:
            subprocess.run(
                ["playwright", "install", "chromium"],
                timeout=180,
            )
        except Exception:
            pass

import threading as _threading
_threading.Thread(target=_ensure_chromium, daemon=True).start()

# Lock so only one SSO attempt runs at a time (warmup thread + request thread race)
_session_lock = _threading.Lock()

PANOPTO_BASE = "https://tau.cloud.panopto.eu"
SINCE_DATE = "2026-03-01T00:00:00.000Z"
NOTES_DIR = tempfile.gettempdir()

# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

_runtime_cookie = ""  # set via /set-cookie page
_sso_last_error = ""  # last SSO failure reason (shown in debug)

UA = ("Mozilla/5.0 (iPad; CPU OS 17_0 like Mac OS X) "
      "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1")


def _try_sso_login():
    """Log in to Panopto via Moodle SSO using a headless Chromium browser.

    Panopto's auth flow requires JavaScript (async Promise API call) so plain
    requests cannot replicate it. Playwright drives a real browser through the
    full SSO redirect chain and hands back the resulting cookies.
    """
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

    global _sso_last_error
    username = os.environ.get("MOODLE_USERNAME", "")
    tau_id   = os.environ.get("MOODLE_ID", "")
    password = os.environ.get("MOODLE_PASSWORD", "")

    if not username or not password:
        _sso_last_error = "Missing MOODLE_USERNAME or MOODLE_PASSWORD"
        return None

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--disable-blink-features=AutomationControlled",
                ],
            )
            # Use a desktop Chrome UA — enterprise SSO (NetIQ/NIDP) often
            # renders different forms for mobile UAs and may reject headless mobile
            desktop_ua = (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
            context = browser.new_context(
                user_agent=desktop_ua,
                extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
            )
            # Mask navigator.webdriver so NIDP bot-detection doesn't flag us
            context.add_init_script(
                "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
            )
            page = context.new_page()

            # 1. Land on Panopto login page — this loads the SSO button
            _sso_last_error = "playwright: navigating to Login.aspx"
            page.goto(
                f"{PANOPTO_BASE}/Panopto/Pages/Auth/Login.aspx?authCAS=Moodle2025",
                wait_until="networkidle",
                timeout=30000,
            )

            # 2. Click the "Login with Moodle" button.
            #    Its onclick adds &instance=Moodle2025 to the URL, which triggers
            #    Panopto.Application.setPanoptoState() — the async JS Promise that
            #    requests can never replicate.
            _sso_last_error = f"playwright: at {page.url[:80]}, clicking Moodle button"
            try:
                page.locator("a[onclick*='Moodle2025'], a[onclick*='Moodle'], button[onclick*='Moodle']").first.click(timeout=10000)
            except PWTimeout:
                # Some deployments auto-redirect; continue anyway
                _sso_last_error += " | button timeout, continuing"

            # 3. Wait for a password field — that's the Moodle/NetIQ login form
            _sso_last_error += f" | waiting for login form (now at {page.url[:60]})"
            try:
                page.wait_for_selector("input[type='password']", timeout=25000)
            except PWTimeout:
                # May already be logged in or at an unexpected page
                _sso_last_error += f" | no password field at {page.url[:80]}"

            # 4. Fill credentials — TAU NetIQ has THREE fields:
            #    [Username/surname] [ID number] [Password]
            if page.locator("input[type='password']").count() > 0:
                _sso_last_error += f" | filling creds at {page.url[:70]}"

                # Collect all visible text inputs in order
                text_inputs = page.locator(
                    "input[type='text'], input[type='number'], input[type='tel'], "
                    "input:not([type]), input[type='email']"
                )
                n_text = text_inputs.count()
                _sso_last_error += f" | text_fields={n_text}"

                if n_text >= 1:
                    text_inputs.nth(0).fill(username)          # Field 1: surname
                if n_text >= 2 and tau_id:
                    text_inputs.nth(1).fill(tau_id)            # Field 2: student ID
                elif n_text >= 2 and not tau_id:
                    _sso_last_error += " | TAU_ID not set!"

                page.locator("input[type='password']").first.fill(password)

                # Click submit button (NIDP forms often ignore Enter key)
                submitted = False
                for btn_sel in ["input[type='submit']", "button[type='submit']",
                                "input[name='loginButton']", "button.btn-primary",
                                "button.btn", "input[value='Login']",
                                "input[value='Log In']", "input[value='Sign In']"]:
                    try:
                        btn = page.locator(btn_sel).first
                        if btn.count() > 0:
                            btn.click(timeout=5000)
                            submitted = True
                            break
                    except Exception:
                        pass
                if not submitted:
                    page.locator("input[type='password']").first.press("Enter")
                _sso_last_error += f" | submitted={submitted}"

                try:
                    page.wait_for_load_state("networkidle", timeout=8000)
                except Exception:
                    pass
                _sso_last_error += f" | post_submit={page.url[:90]}"

            # 5. Wait to land back on Panopto — SAML chain nidp→moodle→panopto
            #    can take 60-90s on TAU infrastructure
            _sso_last_error += " | waiting for Panopto redirect"
            try:
                page.wait_for_url(f"{PANOPTO_BASE}/**", timeout=90000)
            except PWTimeout:
                _sso_last_error += f" | redirect timeout, at {page.url[:100]}"

            # Brief settle so all cookies are written
            page.wait_for_timeout(2000)

            cookies = context.cookies()
            browser.close()

        # Check we got the auth cookie
        has_auth = any(c["name"] == ".ASPXAUTH" for c in cookies)
        if not has_auth:
            _sso_last_error += " | no .ASPXAUTH cookie — auth failed"
            return None

        # Transfer Playwright cookies into a requests.Session
        s = req.Session()
        s.headers["User-Agent"] = UA
        for c in cookies:
            domain = c.get("domain", "").lstrip(".")
            if domain:
                s.cookies.set(c["name"], c["value"], domain=domain)

        _sso_last_error = "SSO SUCCESS (playwright)"
        return s

    except Exception as e:
        _sso_last_error = f"playwright error: {e}"
        return None


def _build_panopto_session():
    """Try SSO first, fall back to pasted cookies."""
    # Auto-login via Moodle SSO
    if os.environ.get("MOODLE_USERNAME") and os.environ.get("MOODLE_PASSWORD"):
        s = _try_sso_login()
        if s:
            return s

    # Fall back to manually pasted cookies
    cookie_str = _runtime_cookie or os.environ.get("PANOPTO_COOKIE", "")
    s = req.Session()
    s.headers["User-Agent"] = UA
    for part in cookie_str.split(";"):
        part = part.strip()
        if "=" in part:
            name, _, value = part.partition("=")
            s.cookies.set(name.strip(), value.strip(), domain="tau.cloud.panopto.eu")
    return s


def get_session():
    with _session_lock:
        if not hasattr(app, "_panopto_session") or app._panopto_session is None:
            app._panopto_session = _build_panopto_session()
    return app._panopto_session


def reset_session():
    with _session_lock:
        app._panopto_session = None


def session_is_ready():
    """True if a session is already cached (doesn't trigger SSO)."""
    return hasattr(app, "_panopto_session") and app._panopto_session is not None


# ---------------------------------------------------------------------------
# Panopto API
# ---------------------------------------------------------------------------

def _parse_ms_date(date_str):
    """Parse Panopto's /Date(ms)/ or ISO string to ISO string."""
    if not date_str:
        return ""
    m = re.match(r"/Date\((-?\d+)", date_str)
    if m:
        from datetime import timezone
        dt = datetime.fromtimestamp(int(m.group(1)) / 1000, tz=timezone.utc)
        return dt.isoformat()
    return date_str


def _parse_webmethod_results(raw):
    """Normalise raw GetSessions result items into our standard dict."""
    results = []
    for item in raw:
        results.append({
            "Id": item.get("DeliveryID") or item.get("Id", ""),
            "Name": item.get("SessionName") or item.get("Name", "Untitled"),
            "StartTime": _parse_ms_date(item.get("StartTime", "")),
            "Duration": item.get("Duration"),
            "DownloadUrl": (item.get("Urls") or {}).get("Download") or item.get("IosVideoUrl"),
            "CaptionDownloadUrl": (item.get("Urls") or {}).get("CaptionsDownload"),
        })
    return results


def _webmethod_headers(csrf, list_url):
    return {
        "X-CSRF-Token": csrf,
        "Accept": "application/json",
        "Content-Type": "application/json; charset=UTF-8",
        "Referer": list_url,
        "Origin": PANOPTO_BASE,
    }


def _webmethod_sessions(s, max_results=100):
    """Call Panopto's internal GetSessions WebMethod.

    TAU runs an older Panopto build. We try several payload shapes in order,
    stopping at the first 200 response. The most common cause of HTTP 500 on
    older installs is including fields that don't exist in that version
    (e.g. sessionListScope was added later).
    """
    list_url = f"{PANOPTO_BASE}/Panopto/Pages/Sessions/List.aspx"
    # Visit the page first to get a fresh csrfToken cookie
    try:
        s.get(list_url, allow_redirects=True, timeout=15)
    except Exception:
        pass
    csrf = unquote(s.cookies.get("csrfToken", ""))
    hdrs = _webmethod_headers(csrf, list_url)
    endpoint = f"{list_url}/GetSessions"

    # Payloads ordered from most-minimal to most-specific.
    # Older Panopto (pre-5.x) doesn't have sessionListScope or bookmarked.
    payloads = [
        # 1. Bare minimum — just query + paging (widest compatibility)
        {"queryParameters": {
            "query": "", "maxResults": max_results, "page": 0,
        }},
        # 2. Add sort fields (still no scope)
        {"queryParameters": {
            "query": "", "sortColumn": 1, "sortAscending": False,
            "maxResults": max_results, "page": 0,
            "startDate": None, "endDate": None, "folderID": None,
        }},
        # 3. Full payload without sessionListScope
        {"queryParameters": {
            "query": "", "sortColumn": 1, "sortAscending": False,
            "maxResults": max_results, "page": 0,
            "startDate": None, "endDate": None, "folderID": None,
            "bookmarked": False,
        }},
        # 4. Scope = 0 (all accessible sessions)
        {"queryParameters": {
            "query": "", "sortColumn": 1, "sortAscending": False,
            "maxResults": max_results, "page": 0,
            "startDate": None, "endDate": None, "folderID": None,
            "bookmarked": False, "sessionListScope": 0,
        }},
        # 5. Scope = 2 (shared with me)
        {"queryParameters": {
            "query": "", "sortColumn": 1, "sortAscending": False,
            "maxResults": max_results, "page": 0,
            "startDate": None, "endDate": None, "folderID": None,
            "bookmarked": False, "sessionListScope": 2,
        }},
    ]

    last_err = ""
    for payload in payloads:
        try:
            r = s.post(endpoint, json=payload, headers=hdrs, timeout=30)
            if r.status_code == 200:
                raw = r.json().get("d", {}).get("Results", [])
                return _parse_webmethod_results(raw), f"webmethod payload={list(payload['queryParameters'].keys())}"
            last_err = f"HTTP {r.status_code}: {r.text[:200]}"
        except Exception as ex:
            last_err = str(ex)

    raise RuntimeError(f"All WebMethod payloads failed. Last error: {last_err}")


def _scrape_list_aspx(s):
    """Last-resort: parse session data embedded in List.aspx HTML.

    Panopto injects a JavaScript object like:
      Panopto.Utils.Data.SessionList.init({"Results":[...],...})
    or stores it in a <script> block as a JSON variable.
    """
    list_url = f"{PANOPTO_BASE}/Panopto/Pages/Sessions/List.aspx"
    r = s.get(list_url, allow_redirects=True, timeout=20)
    if not r.ok:
        raise RuntimeError(f"List.aspx returned {r.status_code}")

    # Look for JSON blob containing "DeliveryID" or "SessionName" keys
    patterns = [
        r'Panopto\.[^(]+\.init\((\{.*?"Results".*?\})\)',
        r'var\s+\w+\s*=\s*(\{.*?"Results".*?\});',
        r'(\{"Results":\[.*?\].*?\})',
    ]
    for pat in patterns:
        m = re.search(pat, r.text, re.DOTALL)
        if m:
            try:
                data = json.loads(m.group(1))
                raw = data.get("Results", [])
                if raw:
                    return _parse_webmethod_results(raw), "html_scrape"
            except Exception:
                continue

    raise RuntimeError("Could not find session JSON in List.aspx HTML")


def _playwright_list_sessions(s):
    """Load List.aspx in a real browser with our session cookies and intercept
    whichever API call the Panopto JavaScript itself makes to fetch sessions.

    This is the nuclear option — we're reusing a browser so we don't have to
    reverse-engineer the API.  Slow (~20s) but works regardless of API version.
    """
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

    pw_cookies = []
    for c in s.cookies:
        pw_cookies.append({
            "name": c.name,
            "value": c.value,
            "domain": "tau.cloud.panopto.eu",
            "path": "/",
        })

    found = []

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
        )
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            )
        )
        context.add_cookies(pw_cookies)
        page = context.new_page()

        def on_response(resp):
            if resp.status != 200:
                return
            url = resp.url
            if not any(k in url for k in ["/GetSessions", "/api/sessions",
                                           "/api/v1/sessions", "/SessionList"]):
                return
            try:
                data = resp.json()
                # WebMethod: {"d": {"Results": [...]}}
                raw = (data.get("d") or {}).get("Results") or []
                if not raw:
                    # REST: {"Results": [...]}
                    raw = data.get("Results") or []
                if raw:
                    found.extend(_parse_webmethod_results(raw))
            except Exception:
                pass

        page.on("response", on_response)

        try:
            page.goto(
                f"{PANOPTO_BASE}/Panopto/Pages/Sessions/List.aspx",
                wait_until="networkidle",
                timeout=35000,
            )
            page.wait_for_timeout(6000)
        except Exception:
            pass

        browser.close()

    return found


def list_shared_sessions():
    s = get_session()
    since = datetime(2026, 3, 1)

    def _filter_since(items):
        """Keep only sessions from March 2026 onwards (client-side filter)."""
        out = []
        for item in items:
            start = item.get("StartTime", "")
            try:
                dt = datetime.fromisoformat(start.replace("Z", "").replace("+00:00", ""))
                if dt >= since:
                    out.append(item)
            except Exception:
                out.append(item)  # keep if we can't parse the date
        return out

    # 1. Internal WebMethod (tries multiple payload shapes)
    try:
        results, _method = _webmethod_sessions(s)
        return _filter_since(results)
    except Exception:
        pass

    # 2. REST API — try multiple version paths
    for api_path in [
        "/Panopto/api/v1/sessions",
        "/Panopto/api/v1.0/sessions",
        "/Panopto/api/sessions",
    ]:
        try:
            r = s.get(
                f"{PANOPTO_BASE}{api_path}",
                params={"isSharedWithMe": "true", "sortField": "StartTime",
                        "sortOrder": "Desc", "pagination[maxResults]": 100},
                timeout=30,
            )
            if r.ok:
                data = r.json()
                return _filter_since(data.get("Results", []))
        except Exception:
            pass

    # 3. Scrape HTML of List.aspx
    try:
        results, _method = _scrape_list_aspx(s)
        return _filter_since(results)
    except Exception:
        pass

    # 4. Use a real browser — intercept whichever API the Panopto JS actually calls
    results = _playwright_list_sessions(s)
    return _filter_since(results)
    # (no raise — empty list is handled in the /lectures route)


def get_session_detail(session_id):
    s = get_session()
    # Try REST API first, fall back to looking in the session list
    r = s.get(f"{PANOPTO_BASE}/Panopto/api/v1/sessions/{session_id}", timeout=20)
    if r.ok:
        return r.json()
    # If REST API fails, fetch from WebMethod and find the matching session
    try:
        all_sessions = _webmethod_sessions(s, max_results=200)
        for sess in all_sessions:
            if sess.get("Id") == session_id:
                return sess
    except Exception:
        pass
    return {}


# ---------------------------------------------------------------------------
# Transcript extraction
# ---------------------------------------------------------------------------

def strip_srt_timestamps(text):
    """Remove SRT/VTT timestamp lines, keep only spoken text."""
    lines = text.splitlines()
    clean = []
    for line in lines:
        line = line.strip()
        # Skip index numbers, timestamp arrows, WEBVTT header, blank lines
        if not line:
            continue
        if line.isdigit():
            continue
        if re.match(r"^\d{2}:\d{2}", line):
            continue
        if line.startswith("WEBVTT"):
            continue
        clean.append(line)
    return " ".join(clean)


def download_and_transcribe(session_id, download_url, caption_url):
    """Return (transcript_text, method_used)."""
    s = get_session()

    # Try captions first — instant and free
    if caption_url:
        try:
            r = s.get(caption_url, timeout=20)
            if r.ok and len(r.text) > 100:
                return strip_srt_timestamps(r.text), "captions"
        except Exception:
            pass

    if not download_url:
        return None, "no_source"

    groq_client = Groq(api_key=os.environ["GROQ_API_KEY"])

    # Download audio to /tmp
    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f:
        tmp_mp4 = f.name
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
        tmp_mp3 = f.name

    try:
        # Stream download
        with s.get(download_url, stream=True, timeout=120) as r:
            r.raise_for_status()
            with open(tmp_mp4, "wb") as out:
                for chunk in r.iter_content(chunk_size=1024 * 1024):
                    out.write(chunk)

        # Extract audio at 64kbps mono 16kHz to keep size small
        subprocess.run(
            ["ffmpeg", "-y", "-i", tmp_mp4, "-vn", "-ar", "16000", "-ac", "1", "-ab", "64k", tmp_mp3],
            check=True, capture_output=True,
        )

        # Get duration in seconds via ffprobe
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", tmp_mp3],
            capture_output=True, text=True, check=True,
        )
        total_seconds = float(probe.stdout.strip())
        file_size = os.path.getsize(tmp_mp3)

        # Split into <20MB chunks for Groq's 25MB limit using ffmpeg
        max_bytes = 20 * 1024 * 1024
        num_chunks = math.ceil(file_size / max_bytes)
        chunk_seconds = math.ceil(total_seconds / num_chunks)

        transcript_parts = []
        for i in range(num_chunks):
            start = i * chunk_seconds
            with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as cf:
                chunk_path = cf.name
            subprocess.run(
                ["ffmpeg", "-y", "-i", tmp_mp3, "-ss", str(start),
                 "-t", str(chunk_seconds), "-c", "copy", chunk_path],
                check=True, capture_output=True,
            )
            try:
                with open(chunk_path, "rb") as audio_file:
                    result = groq_client.audio.transcriptions.create(
                        model="whisper-large-v3",
                        file=audio_file,
                        response_format="text",
                    )
                transcript_parts.append(result if isinstance(result, str) else result.text)
            finally:
                os.unlink(chunk_path)

        return " ".join(transcript_parts), "whisper"

    finally:
        for p in (tmp_mp4, tmp_mp3):
            try:
                os.unlink(p)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Notes generation
# ---------------------------------------------------------------------------

NOTES_PROMPT = """You are an expert academic note-taker. Given the transcript of a university lecture, produce detailed structured study notes.

Lecture title: {title}
Date: {date}

Transcript:
{transcript}

Produce notes in this EXACT markdown format:

# {title}
**Date:** {date}

## Overview
[3-4 sentences summarising what the lecture covered and why it matters]

## Key Topics
[For each major topic covered, use a ### heading and 4-8 bullet points explaining the key ideas, mechanisms, examples, and implications. Be specific and detailed enough that a student could study from these notes alone.]

## Definitions & Key Terms
[A bullet list of important terms introduced, each formatted as **Term**: clear definition]

## Key Takeaways
[A numbered list of the 5-8 most important things to remember from this lecture]

Be thorough. Use the actual content from the transcript. Do not add padding or repeat yourself."""


def generate_notes(title, date, transcript):
    groq_client = Groq(api_key=os.environ["GROQ_API_KEY"])

    # Trim transcript to ~12000 words to stay within context
    words = transcript.split()
    if len(words) > 12000:
        transcript = " ".join(words[:12000]) + "\n[transcript trimmed for length]"

    prompt = NOTES_PROMPT.format(title=title, date=date, transcript=transcript)
    response = groq_client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.3,
        max_tokens=4096,
    )
    return response.choices[0].message.content


def notes_path(session_id):
    return os.path.join(NOTES_DIR, f"notes_{session_id}.md")


# ---------------------------------------------------------------------------
# HTML helpers
# ---------------------------------------------------------------------------

def _fmt_duration(seconds):
    if not seconds:
        return "—"
    seconds = int(seconds)
    h, m = divmod(seconds, 3600)
    m, s = divmod(m, 60)
    if h:
        return f"{h}h {m:02d}m"
    return f"{m}m {s:02d}s"


def _fmt_date(iso):
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00").replace("+00:00", ""))
        return dt.strftime("%-d %b %Y")
    except Exception:
        return iso[:10] if iso else "—"


PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Lecture Notes</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    background: #0f172a; color: #e2e8f0;
    padding: 16px env(safe-area-inset-right) 32px env(safe-area-inset-left);
    min-height: 100vh;
  }}
  h1 {{ font-size: 1.5rem; font-weight: 700; color: #f8fafc; margin-bottom: 4px; }}
  .sub {{ color: #94a3b8; font-size: 0.875rem; margin-bottom: 24px; }}
  .card {{
    background: #1e293b; border-radius: 12px; padding: 16px;
    margin-bottom: 12px; border: 1px solid #334155;
  }}
  .card h3 {{ font-size: 1rem; font-weight: 600; color: #f1f5f9; margin-bottom: 4px; }}
  .meta {{ font-size: 0.8rem; color: #64748b; margin-bottom: 12px; }}
  .btn {{
    display: inline-block; padding: 10px 20px; border-radius: 8px;
    font-size: 0.9rem; font-weight: 600; cursor: pointer;
    border: none; text-decoration: none; text-align: center;
  }}
  .btn-primary {{ background: #6366f1; color: white; }}
  .btn-success {{ background: #10b981; color: white; }}
  .btn-sm {{ padding: 7px 14px; font-size: 0.8rem; }}
  .btn-full {{ display: block; width: 100%; margin-top: 8px; }}
  .badge {{
    display: inline-block; padding: 2px 8px; border-radius: 99px;
    font-size: 0.7rem; font-weight: 600; text-transform: uppercase;
  }}
  .badge-done {{ background: #065f46; color: #6ee7b7; }}
  .badge-ready {{ background: #1e40af; color: #93c5fd; }}
  .alert {{
    background: #1e293b; border: 1px solid #f59e0b;
    border-radius: 8px; padding: 12px 16px; margin-bottom: 16px;
    color: #fcd34d; font-size: 0.875rem;
  }}
  .alert-err {{ border-color: #ef4444; color: #fca5a5; }}
  form {{ margin: 0; }}
  input[type=text], input[type=password], input[type=url] {{
    width: 100%; padding: 10px 12px; border-radius: 8px;
    background: #0f172a; border: 1px solid #334155;
    color: #e2e8f0; font-size: 1rem; margin-bottom: 12px;
  }}
  label {{ display: block; font-size: 0.85rem; color: #94a3b8; margin-bottom: 4px; }}
  .spinner {{
    display: inline-block; width: 16px; height: 16px;
    border: 2px solid rgba(255,255,255,0.3); border-top-color: white;
    border-radius: 50%; animation: spin 0.8s linear infinite;
    vertical-align: middle; margin-right: 6px;
  }}
  @keyframes spin {{ to {{ transform: rotate(360deg); }} }}
</style>
</head>
<body>
{body}
</body>
</html>"""


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/health")
def health():
    """Instant status — no SSO triggered. Visit this to confirm code version."""
    import html as _h
    pw_cache = os.path.join(os.path.expanduser("~"), ".cache", "ms-playwright")
    try:
        browsers = os.listdir(pw_cache) if os.path.isdir(pw_cache) else []
    except Exception:
        browsers = []
    status = {
        "version": "2026-04-14-v8",
        "session_ready": session_is_ready(),
        "sso_last_error": _sso_last_error,
        "pw_browsers": browsers,
        "lock_held": _session_lock.locked(),
        "env_username_set": bool(os.environ.get("MOODLE_USERNAME")),
        "env_password_set": bool(os.environ.get("MOODLE_PASSWORD")),
        "env_groq_set": bool(os.environ.get("GROQ_API_KEY")),
    }
    lines = "\n".join(f"{k}: {v}" for k, v in status.items())
    body = f"""
    <h1>Health</h1>
    <div class="card"><pre style="white-space:pre-wrap;font-size:0.8rem;color:#94a3b8">{_h.escape(lines)}</pre></div>
    <a href="/lectures" class="btn btn-primary" style="margin-top:12px">Go to Lectures</a>
    &nbsp;<a href="/debug" class="btn btn-sm" style="color:#64748b">Debug</a>"""
    return PAGE.format(body=body)


@app.route("/")
def index():
    has_auto_auth = (os.environ.get("MOODLE_USERNAME") and os.environ.get("MOODLE_PASSWORD"))
    has_manual_cookie = _runtime_cookie or os.environ.get("PANOPTO_COOKIE", "")

    if not has_auto_auth and not has_manual_cookie:
        body = """
        <h1>Lecture Notes</h1>
        <p class="sub">Set your Moodle credentials in Railway Variables to get started.</p>
        <div class="alert">
          Add these variables in Railway → Variables:<br><br>
          <code>MOODLE_USERNAME</code> — your TAU Moodle username<br>
          <code>MOODLE_PASSWORD</code> — your TAU Moodle password<br>
          <code>GROQ_API_KEY</code> — from console.groq.com (free)<br>
          <code>SECRET_KEY</code> — any random string<br><br>
          Railway will redeploy automatically. Then come back here.
        </div>
        <p style="text-align:center;margin-top:16px">
          <a href="/set-cookie" class="btn btn-sm" style="color:#64748b">Or paste cookies manually</a>
        </p>"""
        return PAGE.format(body=body)

    if not os.environ.get("GROQ_API_KEY"):
        body = '<h1>Lecture Notes</h1><div class="alert alert-err">Missing <code>GROQ_API_KEY</code> — add it in Railway Variables.</div>'
        return PAGE.format(body=body)

    return redirect(url_for("lectures"))


@app.route("/set-cookie", methods=["GET", "POST"])
def set_cookie():
    global _runtime_cookie
    if request.method == "POST":
        _runtime_cookie = request.form.get("cookies", "").strip()
        reset_session()
        return redirect(url_for("lectures"))
    body = """
    <h1>Update Cookies</h1>
    <p class="sub">Paste your Panopto cookies from Chrome DevTools here.<br>
    No need to touch Railway — just paste and tap Save.</p>
    <div class="alert" style="border-color:#6366f1;color:#a5b4fc">
      In Chrome: log into tau.cloud.panopto.eu → right-click → Inspect →
      Application tab → Cookies → tau.cloud.panopto.eu →
      copy each cookie's Name and Value.
    </div>
    <form method="post">
      <label>Paste all cookies as: Name=Value; Name=Value; ...</label>
      <textarea name="cookies" rows="6" style="width:100%;padding:10px;border-radius:8px;background:#0f172a;border:1px solid #334155;color:#e2e8f0;font-size:0.8rem;margin-bottom:12px;font-family:monospace"></textarea>
      <button type="submit" class="btn btn-primary btn-full">Save &amp; Connect</button>
    </form>
    <br><a href="/" class="btn btn-sm" style="color:#64748b">Cancel</a>"""
    return PAGE.format(body=body)


@app.route("/debug")
def debug():
    """Auth diagnostics — SSO status, cookies, and WebMethod probe."""
    import html as html_mod
    out = {}
    try:
        if request.args.get("fresh"):
            reset_session()
        s = get_session()
        list_url = f"{PANOPTO_BASE}/Panopto/Pages/Sessions/List.aspx"
        out["sso_status"] = _sso_last_error
        out["cookies"] = list(s.cookies.keys())
        out["has_aspxauth"] = ".ASPXAUTH" in [c.name for c in s.cookies]

        # Warmup: visit List.aspx so Panopto issues a fresh csrfToken
        try:
            warmup = s.get(list_url, allow_redirects=True, timeout=15)
            out["list_aspx"] = f"HTTP {warmup.status_code} url={warmup.url[:80]}"
        except Exception as ex:
            out["list_aspx"] = f"ERR: {ex}"
        csrf = unquote(s.cookies.get("csrfToken", ""))
        out["csrf_token"] = csrf[:40] + "..." if len(csrf) > 40 else (csrf or "(empty)")

        hdrs = _webmethod_headers(csrf, list_url)
        endpoint = f"{list_url}/GetSessions"

        # Try all payload variations so we can see which one works
        wm_payloads = {
            "wm_bare":       {"queryParameters": {"query": "", "maxResults": 5, "page": 0}},
            "wm_sort":       {"queryParameters": {"query": "", "sortColumn": 1, "sortAscending": False,
                               "maxResults": 5, "page": 0, "startDate": None, "endDate": None, "folderID": None}},
            "wm_no_scope":   {"queryParameters": {"query": "", "sortColumn": 1, "sortAscending": False,
                               "maxResults": 5, "page": 0, "startDate": None, "endDate": None,
                               "folderID": None, "bookmarked": False}},
            "wm_scope0":     {"queryParameters": {"query": "", "sortColumn": 1, "sortAscending": False,
                               "maxResults": 5, "page": 0, "startDate": None, "endDate": None,
                               "folderID": None, "bookmarked": False, "sessionListScope": 0}},
            "wm_scope2":     {"queryParameters": {"query": "", "sortColumn": 1, "sortAscending": False,
                               "maxResults": 5, "page": 0, "startDate": None, "endDate": None,
                               "folderID": None, "bookmarked": False, "sessionListScope": 2}},
        }
        for label, payload in wm_payloads.items():
            try:
                rv = s.post(endpoint, json=payload, headers=hdrs, timeout=15)
                # Show status + first 400 chars of body (enough to see error message or result count)
                out[label] = f"HTTP {rv.status_code} | {rv.text[:400]}"
            except Exception as ex:
                out[label] = f"ERR: {ex}"

        # REST API — try the unversioned endpoint with different parameter shapes
        # The unversioned /api/sessions returns "Cannot read sessions without a valid filter"
        # when no params are sent — it IS alive, we just need the right params.
        api_base = f"{PANOPTO_BASE}/Panopto/api/sessions"
        for label, params in [
            ("api_query",       {"query": ""}),
            ("api_search",      {"searchQuery": ""}),
            ("api_scope0_q",    {"query": "", "sessionListScopeType": "0", "maxResults": "5", "page": "0"}),
            ("api_scope2_q",    {"query": "", "sessionListScopeType": "2", "maxResults": "5", "page": "0"}),
            ("api_startidx",    {"searchQuery": "", "startIndex": "0", "count": "5"}),
            ("api_v1",          {"isSharedWithMe": "true", "maxResults": "5"}),
        ]:
            url = api_base if label != "api_v1" else f"{PANOPTO_BASE}/Panopto/api/v1/sessions"
            try:
                ra = s.get(url, params=params, timeout=15)
                out[label] = f"HTTP {ra.status_code} | {ra.text[:300]}"
            except Exception as ex:
                out[label] = f"ERR: {ex}"

        # HTML scrape attempt
        try:
            results, method = _scrape_list_aspx(s)
            out["html_scrape"] = f"OK via {method}: {len(results)} sessions"
        except Exception as ex:
            out["html_scrape"] = f"ERR: {ex}"

        lines = "\n\n".join(f"{k}:\n  {html_mod.escape(str(v))}" for k, v in out.items())
        body = f"""
        <h1>Debug</h1>
        <div class="card"><pre style="white-space:pre-wrap;font-size:0.75rem;color:#94a3b8">{lines}</pre></div>
        <a href="/lectures" class="btn btn-primary">Lectures</a>
        &nbsp;<a href="/debug?fresh=1" class="btn btn-sm" style="color:#94a3b8">Re-auth</a>
        &nbsp;<a href="/capture-api" class="btn btn-sm" style="color:#a78bfa">Capture Browser API</a>
        &nbsp;<a href="/set-cookie" class="btn btn-sm" style="color:#94a3b8">Paste Cookies</a>"""
    except Exception as e:
        body = f'<h1>Debug Error</h1><div class="alert alert-err">{html_mod.escape(str(e))}</div>'
    return PAGE.format(body=body)


@app.route("/capture-api")
def capture_api():
    """Load List.aspx inside a real Playwright browser (with session cookies) and
    intercept every API call it makes.  This tells us the exact URLs + payloads
    that work with this Panopto instance."""
    import html as html_mod
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

    captured_req = []
    captured_res = []
    s = get_session()

    # Convert requests.Session cookies → Playwright cookie dicts
    pw_cookies = []
    for c in s.cookies:
        pw_cookies.append({
            "name": c.name,
            "value": c.value,
            "domain": "tau.cloud.panopto.eu",
            "path": getattr(c, "path", "/") or "/",
        })

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
            )
            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                )
            )
            context.add_cookies(pw_cookies)
            page = context.new_page()

            def on_request(req_obj):
                url = req_obj.url
                if any(k in url for k in ["/Panopto/api/", "/GetSessions", "/GetFolders",
                                           "/SessionList", "/sessions", "Session"]):
                    captured_req.append({
                        "method": req_obj.method,
                        "url": url[:250],
                        "post": (req_obj.post_data or "")[:400],
                        "headers": {k: v for k, v in req_obj.headers.items()
                                    if k.lower() in ("content-type", "x-csrf-token", "accept")},
                    })

            def on_response(resp_obj):
                url = resp_obj.url
                if any(k in url for k in ["/Panopto/api/", "/GetSessions", "/GetFolders",
                                           "/SessionList", "/sessions"]):
                    try:
                        body_text = resp_obj.text()[:400]
                    except Exception:
                        body_text = "(unreadable)"
                    captured_res.append(f"HTTP {resp_obj.status} {url[:200]}: {body_text}")

            page.on("request", on_request)
            page.on("response", on_response)

            try:
                page.goto(
                    f"{PANOPTO_BASE}/Panopto/Pages/Sessions/List.aspx",
                    wait_until="networkidle",
                    timeout=30000,
                )
                # Give extra time for deferred AJAX calls
                page.wait_for_timeout(6000)
            except Exception as nav_err:
                captured_req.append({"method": "NAV_ERR", "url": str(nav_err)[:200],
                                      "post": "", "headers": {}})

            browser.close()

        # Also try to extract session names from what we captured
        session_names = []
        for entry in captured_res:
            if '"SessionName"' in entry or '"Name"' in entry:
                try:
                    # Entry format: "HTTP 200 url: body..."
                    body_start = entry.index(": ", entry.index("HTTP")) + 2
                    raw_json = entry[body_start:]
                    data = json.loads(raw_json)
                    items = (data.get("d") or {}).get("Results") or data.get("Results") or []
                    for it in items[:5]:
                        name = it.get("SessionName") or it.get("Name", "")
                        if name:
                            session_names.append(name)
                except Exception:
                    pass

        result = {
            "sessions_found": session_names or "(none — see responses for raw data)",
            "requests": captured_req,
            "responses": captured_res,
        }
        pre = html_mod.escape(json.dumps(result, indent=2))
        body = f"""
        <h1>Browser API Capture</h1>
        <p class="sub">API calls made by List.aspx in a real Chromium browser with your session cookies.</p>
        <div class="card"><pre style="white-space:pre-wrap;font-size:0.7rem;color:#94a3b8">{pre}</pre></div>
        <a href="/lectures" class="btn btn-primary">Try Lectures Now</a>
        &nbsp;<a href="/debug" class="btn btn-sm" style="color:#94a3b8">Back to Debug</a>"""
    except Exception as e:
        body = f'<h1>Capture Error</h1><div class="alert alert-err">{html_mod.escape(str(e))}</div>'
    return PAGE.format(body=body)


@app.route("/lectures")
def lectures():
    error = request.args.get("error")

    # If SSO is still in progress (lock held by warmup thread), show a spinner
    # instead of blocking the HTTP connection for 60+ seconds.
    if not session_is_ready() and _session_lock.locked():
        body = """
        <h1>Lecture Notes</h1>
        <p class="sub">Logging into Panopto via Moodle SSO&hellip;</p>
        <div class="card">
          <div><span class="spinner"></span> Connecting, please wait&hellip;</div>
          <div class="meta" style="margin-top:8px">This takes about 30&ndash;60 seconds on first load.</div>
        </div>
        <meta http-equiv="refresh" content="6">"""
        return PAGE.format(body=body)

    try:
        sessions = list_shared_sessions()
    except Exception as e:
        reset_session()
        body = f"""
        <h1>Lecture Notes</h1>
        <div class="alert alert-err">Could not connect to Panopto: {e}<br><br>
        Check your MOODLE_URL / credentials env vars, then <a href="/lectures" style="color:#fca5a5">retry</a>.</div>"""
        return PAGE.format(body=body)

    if not sessions:
        body = """
        <h1>Lecture Notes</h1>
        <p class="sub">No lectures found shared with you since March 2026.</p>
        <div class="alert">Make sure your Moodle credentials are correct and lectures have been shared with your account.</div>
        <a href="/lectures" class="btn btn-primary">Refresh</a>"""
        return PAGE.format(body=body)

    cards = []
    has_pending = False
    for item in sessions:
        sid = item.get("Id", "")
        title = item.get("Name", "Untitled")
        date = _fmt_date(item.get("StartTime", ""))
        dur = _fmt_duration(item.get("Duration"))
        has_notes = os.path.exists(notes_path(sid))

        if has_notes:
            badge = '<span class="badge badge-done">Notes ready</span>'
            action = f'<a href="/download/{sid}" class="btn btn-success btn-sm btn-full">Download .md</a>'
        else:
            has_pending = True
            badge = ""
            action = f"""<form method="post" action="/process/{sid}">
              <button class="btn btn-primary btn-sm btn-full" type="submit">Generate Notes</button>
            </form>"""

        cards.append(f"""
        <div class="card">
          <h3>{title}</h3>
          <div class="meta">{date} &middot; {dur} {badge}</div>
          {action}
        </div>""")

    gen_all_btn = ""
    if has_pending:
        ids = [s["Id"] for s in sessions if not os.path.exists(notes_path(s["Id"]))]
        ids_json = json.dumps(ids)
        gen_all_btn = f"""
        <form method="post" action="/process-all">
          <input type="hidden" name="ids" value='{ids_json}'>
          <button class="btn btn-primary btn-full" type="submit">
            Generate All Pending Notes ({len(ids)} lectures)
          </button>
        </form>
        <br>"""

    err_html = f'<div class="alert alert-err">{error}</div>' if error else ""
    body = f"""
    <h1>Lecture Notes</h1>
    <p class="sub">{len(sessions)} lectures shared with you since March 2026</p>
    {err_html}
    {gen_all_btn}
    {''.join(cards)}"""
    return PAGE.format(body=body)


@app.route("/process/<session_id>", methods=["POST"])
def process(session_id):
    def stream():
        yield PAGE.format(body=f"""
        <h1>Generating Notes…</h1>
        <p class="sub">This may take a few minutes for long lectures.</p>
        <div class="card">
          <div><span class="spinner"></span> Fetching lecture details…</div>
        </div>""")

        try:
            detail = get_session_detail(session_id)
            title = detail.get("Name", "Untitled")
            date = _fmt_date(detail.get("StartTime", ""))
            download_url = detail.get("DownloadUrl") or detail.get("Urls", {}).get("DownloadUrl")
            caption_url = detail.get("CaptionDownloadUrl") or detail.get("Urls", {}).get("CaptionDownloadUrl")

            transcript, method = download_and_transcribe(session_id, download_url, caption_url)
            if not transcript:
                yield PAGE.format(body=f"""
                <h1>Error</h1>
                <div class="alert alert-err">Could not retrieve transcript for this lecture.
                The video may not have captions and no download URL was available.</div>
                <a href="/lectures" class="btn btn-primary">Back</a>""")
                return

            notes_md = generate_notes(title, date, transcript)
            with open(notes_path(session_id), "w") as f:
                f.write(notes_md)

            source_label = "auto-captions" if method == "captions" else "Whisper transcription"
            yield PAGE.format(body=f"""
            <h1>Notes Ready!</h1>
            <p class="sub">Generated from {source_label}</p>
            <div class="card">
              <h3>{title}</h3>
              <div class="meta">{date}</div>
              <a href="/download/{session_id}" class="btn btn-success btn-full">Download .md file</a>
            </div>
            <br>
            <a href="/lectures" class="btn btn-primary btn-full">Back to all lectures</a>""")

        except Exception as e:
            yield PAGE.format(body=f"""
            <h1>Error</h1>
            <div class="alert alert-err">{e}</div>
            <a href="/lectures" class="btn btn-primary">Back</a>""")

    return Response(stream(), content_type="text/html")


@app.route("/process-all", methods=["POST"])
def process_all():
    ids = json.loads(request.form.get("ids", "[]"))

    def stream():
        total = len(ids)
        completed = []
        failed = []

        for i, session_id in enumerate(ids, 1):
            progress_html = f"""
            <h1>Generating All Notes…</h1>
            <p class="sub">Processing {i} of {total} — please keep this page open</p>
            <div class="card">
              <div><span class="spinner"></span> Working on lecture {i}/{total}…</div>
              <div class="meta" style="margin-top:8px">Completed: {len(completed)} &middot; Failed: {len(failed)}</div>
            </div>"""
            yield PAGE.format(body=progress_html)

            try:
                detail = get_session_detail(session_id)
                title = detail.get("Name", "Untitled")
                date = _fmt_date(detail.get("StartTime", ""))
                download_url = detail.get("DownloadUrl") or detail.get("Urls", {}).get("DownloadUrl")
                caption_url = detail.get("CaptionDownloadUrl") or detail.get("Urls", {}).get("CaptionDownloadUrl")

                transcript, _ = download_and_transcribe(session_id, download_url, caption_url)
                if transcript:
                    notes_md = generate_notes(title, date, transcript)
                    with open(notes_path(session_id), "w") as f:
                        f.write(notes_md)
                    completed.append(title)
                else:
                    failed.append(title)
            except Exception:
                failed.append(session_id)

        done_list = "".join(f"<li>{t}</li>" for t in completed)
        fail_list = "".join(f"<li>{t}</li>" for t in failed)
        fail_section = f'<div class="alert alert-err"><strong>Failed:</strong><ul>{fail_list}</ul></div>' if failed else ""

        yield PAGE.format(body=f"""
        <h1>All Done!</h1>
        <p class="sub">{len(completed)} of {total} lectures processed</p>
        {fail_section}
        <div class="card">
          <strong style="color:#6ee7b7">Completed:</strong>
          <ul style="margin-top:8px;padding-left:16px;font-size:0.875rem">{done_list}</ul>
        </div>
        <br>
        <a href="/lectures" class="btn btn-success btn-full">Back to lectures to download</a>""")

    return Response(stream(), content_type="text/html")


@app.route("/download/<session_id>")
def download(session_id):
    path = notes_path(session_id)
    if not os.path.exists(path):
        return redirect(url_for("lectures", error="Notes file not found. Please generate notes first."))
    return send_file(
        path,
        as_attachment=True,
        download_name=f"notes_{session_id[:8]}.md",
        mimetype="text/markdown",
    )


def _startup_warmup():
    """Pre-warm the Panopto session so the first user request is instant.

    Waits 8 seconds for the app to fully start (and for _ensure_chromium to
    kick off), then triggers SSO in the background. By the time a user opens
    the app URL the session is usually already cached.
    """
    import time
    time.sleep(8)
    if os.environ.get("MOODLE_USERNAME") or os.environ.get("PANOPTO_COOKIE") or _runtime_cookie:
        try:
            get_session()
        except Exception:
            pass

_threading.Thread(target=_startup_warmup, daemon=True).start()


if __name__ == "__main__":
    app.run(debug=True, port=5000)
