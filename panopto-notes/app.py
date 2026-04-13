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
    """Attempt Panopto → Moodle → NetIQ SSO. Returns requests.Session or None."""
    from bs4 import BeautifulSoup
    from urllib.parse import urljoin

    global _sso_last_error
    username = os.environ.get("MOODLE_USERNAME", "")
    moodle_id = os.environ.get("MOODLE_ID", "")
    password = os.environ.get("MOODLE_PASSWORD", "")
    if not username or not password:
        _sso_last_error = "MOODLE_USERNAME or MOODLE_PASSWORD not set"
        return None

    s = req.Session()
    s.headers["User-Agent"] = UA

    # 1. Kick off SSO — try Moodle2025 first (TAU's provider name), then MOODLE
    r = None
    for cas_name in ["Moodle2025", "MOODLE", "Moodle"]:
        try:
            r = s.get(f"{PANOPTO_BASE}/Panopto/Pages/Auth/Login.aspx",
                      params={"authCAS": cas_name}, allow_redirects=True, timeout=20)
            # If we got redirected away from Panopto, we've found the right provider
            if "panopto" not in r.url.lower() or "nidp" in r.url.lower() or "moodle" in r.url.lower():
                _sso_last_error = f"SSO redirected to: {r.url[:80]} using authCAS={cas_name}"
                break
        except Exception as e:
            _sso_last_error = f"SSO start error ({cas_name}): {e}"
            continue

    if r is None:
        return None

    _sso_last_error = f"landed at: {r.url[:80]}"

    # If still on Panopto's login page, try clicking the Moodle button via form submit
    if "panopto" in r.url.lower() and "nidp" not in r.url.lower():
        soup0 = BeautifulSoup(r.text, "html.parser")
        form0 = soup0.find("form")
        # Look for Moodle link in page
        moodle_href = None
        for a in soup0.find_all("a", href=True):
            if "moodle" in a["href"].lower() or "cas" in a["href"].lower():
                moodle_href = a["href"]
                break
        # Look in script tags for redirect URL
        if not moodle_href:
            for script in soup0.find_all("script"):
                src = script.string or ""
                m = re.search(r"['\"]([^'\"]*(?:moodle|nidp|cas)[^'\"]*)['\"]", src, re.IGNORECASE)
                if m and m.group(1).startswith("/"):
                    moodle_href = m.group(1)
                    break
        if moodle_href:
            if not moodle_href.startswith("http"):
                from urllib.parse import urljoin
                moodle_href = urljoin(r.url, moodle_href)
            try:
                r = s.get(moodle_href, allow_redirects=True, timeout=20)
                _sso_last_error += f" | followed link to: {r.url[:80]}"
            except Exception as e:
                _sso_last_error += f" | link follow error: {e}"
        elif form0:
            # Try submitting the ViewState form with Moodle event target guesses
            action0 = form0.get("action", r.url)
            if not action0.startswith("http"):
                from urllib.parse import urljoin
                action0 = urljoin(r.url, action0)
            data0 = {inp["name"]: inp.get("value", "")
                     for inp in form0.find_all("input") if inp.get("name")}
            # Try known ASP.NET event target names for Moodle provider
            for evt in ["ctl00$PageContentPlaceholder$loginControl$signInWithMoodle",
                        "ctl00$PageContentPlaceholder$loginControl$moodleBtn",
                        "ctl00$PageContentPlaceholder$loginControl$externalLogin"]:
                data0["__EVENTTARGET"] = evt
                data0["ctl00$PageContentPlaceholder$loginControl$forceStateChanged"] = "true"
                try:
                    r2 = s.post(action0, data=data0, allow_redirects=True, timeout=20)
                    if "nidp" in r2.url or "moodle" in r2.url.lower():
                        r = r2
                        _sso_last_error += f" | form submit→{r.url[:60]}"
                        break
                except Exception:
                    pass

    # 2. Find and fill the login form
    soup = BeautifulSoup(r.text, "html.parser")
    form = soup.find("form")
    if not form:
        _sso_last_error = f"No form at {r.url[:80]}. Snippet: {r.text[:150]}"
        return None

    action = form.get("action", r.url)
    if not action.startswith("http"):
        action = urljoin(r.url, action)

    data = {inp["name"]: inp.get("value", "")
            for inp in form.find_all("input") if inp.get("name")}

    text_fields = [inp["name"] for inp in form.find_all("input")
                   if inp.get("name") and inp.get("type", "text").lower() in ("text", "email", "")]
    pass_fields = [inp["name"] for inp in form.find_all("input")
                   if inp.get("name") and inp.get("type", "").lower() == "password"]

    # Fill username (first text field or known names)
    filled = False
    for fname in ["Ecom_User_ID", "username", "loginname", "j_username", "user"]:
        if fname in data:
            data[fname] = username
            filled = True
            break
    if not filled and text_fields:
        data[text_fields[0]] = username

    # Fill student ID into second text field if present
    if moodle_id and len(text_fields) >= 2:
        data[text_fields[1]] = moodle_id

    # Fill password
    filled = False
    for fname in ["Ecom_Password", "password", "passwd", "j_password"]:
        if fname in data:
            data[fname] = password
            filled = True
            break
    if not filled and pass_fields:
        data[pass_fields[0]] = password

    _sso_last_error += f" | form→{action[:60]} fields={list(data.keys())}"

    try:
        r2 = s.post(action, data=data, allow_redirects=True, timeout=30)
    except Exception as e:
        _sso_last_error += f" | submit error: {e}"
        return None

    _sso_last_error += f" | after submit: {r2.url[:60]}"

    # 3. Follow any SAML/CAS relay forms (hidden auto-submit)
    for _ in range(4):
        if any(c.name == ".ASPXAUTH" for c in s.cookies):
            break
        soup2 = BeautifulSoup(r2.text, "html.parser")
        relay = soup2.find("form")
        if not relay:
            break
        ra = relay.get("action", "")
        if not ra:
            break
        if not ra.startswith("http"):
            ra = urljoin(r2.url, ra)
        rd = {inp["name"]: inp.get("value", "")
              for inp in relay.find_all("input") if inp.get("name")}
        if not any(k in rd for k in ("SAMLResponse", "lt", "ticket", "RelayState", "execution")):
            break
        try:
            r2 = s.post(ra, data=rd, allow_redirects=True, timeout=20)
            _sso_last_error += f" | relay→{r2.url[:60]}"
        except Exception:
            break

    if any(c.name == ".ASPXAUTH" for c in s.cookies):
        _sso_last_error = "SSO SUCCESS"
        return s

    _sso_last_error += f" | FAILED — cookies: {[c.name for c in s.cookies]}"
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
    if not hasattr(app, "_panopto_session") or app._panopto_session is None:
        app._panopto_session = _build_panopto_session()
    return app._panopto_session


def reset_session():
    app._panopto_session = None


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


def _webmethod_sessions(s, max_results=100):
    """Call Panopto's internal GetSessions WebMethod (what the web app uses)."""
    csrf = unquote(s.cookies.get("csrfToken", ""))
    payload = {
        "queryParameters": {
            "query": "",
            "sortColumn": 1,
            "sortAscending": False,
            "maxResults": max_results,
            "page": 0,
            "startDate": SINCE_DATE,
            "endDate": None,
            "folderID": None,
            "bookmarked": False,
            "sessionListScope": 2,  # 2 = Shared with me
        }
    }
    list_url = f"{PANOPTO_BASE}/Panopto/Pages/Sessions/List.aspx"
    r = s.post(
        f"{list_url}/GetSessions",
        json=payload,
        headers={
            "X-CSRF-Token": csrf,
            "Accept": "application/json",
            "Content-Type": "application/json; charset=UTF-8",
            "Referer": list_url,
            "Origin": PANOPTO_BASE,
        },
        timeout=30,
    )
    r.raise_for_status()
    raw = r.json().get("d", {}).get("Results", [])
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


def list_shared_sessions():
    s = get_session()

    # Try the internal WebMethod first (works when REST API version is unsupported)
    try:
        results = _webmethod_sessions(s)
        if results is not None:
            return results
    except Exception:
        pass

    # Fall back to REST API v1
    params = {
        "isSharedWithMe": "true",
        "sortField": "StartTime",
        "sortOrder": "Desc",
        "pagination[maxResults]": 100,
        "minStartDate": SINCE_DATE,
    }
    r = s.get(f"{PANOPTO_BASE}/Panopto/api/v1/sessions", params=params, timeout=30)
    if r.status_code == 401:
        reset_session()
        s = get_session()
        r = s.get(f"{PANOPTO_BASE}/Panopto/api/v1/sessions", params=params, timeout=30)
    data = r.json()
    results = data.get("Results", [])
    since = datetime(2026, 3, 1)
    filtered = []
    for item in results:
        start = item.get("StartTime", "")
        try:
            dt = datetime.fromisoformat(start.replace("Z", "+00:00").replace("+00:00", ""))
            if dt >= since:
                filtered.append(item)
        except Exception:
            filtered.append(item)
    return filtered


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

@app.route("/")
def index():
    if not os.environ.get("PANOPTO_COOKIE"):
        body = """
        <h1>Lecture Notes</h1>
        <p class="sub">One-time setup: get your Panopto session cookie from Safari.</p>
        <div class="alert">
          <strong>Step 1</strong> — Open <code>tau.cloud.panopto.eu</code> in Safari and log in normally.<br><br>
          <strong>Step 2</strong> — Bookmark any page (tap the share button &#x2191; → Add Bookmark).<br><br>
          <strong>Step 3</strong> — Open Bookmarks, find that bookmark, tap Edit, and replace its URL with exactly:<br>
          <code style="word-break:break-all">javascript:prompt('Copy all of this:',document.cookie)</code><br><br>
          <strong>Step 4</strong> — Go back to the Panopto page (still logged in), then open Bookmarks and tap that bookmark.<br><br>
          <strong>Step 5</strong> — A dialog shows your cookies. Select all, copy.<br><br>
          <strong>Step 6</strong> — In Railway → Variables, add <code>PANOPTO_COOKIE</code> and paste.<br><br>
          Railway will redeploy automatically — then come back here.
        </div>
        <div class="alert" style="border-color:#6366f1;color:#a5b4fc;margin-top:12px">
          Also make sure <code>GROQ_API_KEY</code> and <code>SECRET_KEY</code> are set in Railway Variables.
        </div>"""
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
    """Fast auth check — tests WebMethod with Referer header."""
    out = {}
    try:
        s = get_session()
        csrf = unquote(s.cookies.get("csrfToken", ""))  # decode %2f → /
        list_url = f"{PANOPTO_BASE}/Panopto/Pages/Sessions/List.aspx"
        hdrs = {
            "X-CSRF-Token": csrf,
            "Accept": "application/json",
            "Content-Type": "application/json; charset=UTF-8",
            "Referer": list_url,
            "Origin": PANOPTO_BASE,
        }
        out["sso_status"] = _sso_last_error
        out["cookies"] = list(s.cookies.keys())
        out["has_aspxauth"] = ".ASPXAUTH" in [c.name for c in s.cookies]

        # Show Login.aspx HTML so we can see what's on the page
        try:
            rl = s.get(f"{PANOPTO_BASE}/Panopto/Pages/Auth/Login.aspx",
                       params={"authCAS": "Moodle2025"}, allow_redirects=True, timeout=10)
            out["login_page_url"] = rl.url[:100]
            out["login_page_html"] = rl.text[:800]
        except Exception as ex:
            out["login_page_html"] = f"ERR: {ex}"

        # Test WebMethod with decoded CSRF token
        try:
            payload = {"queryParameters": {"query": "", "sortColumn": 1, "sortAscending": False,
                       "maxResults": 5, "page": 0, "startDate": None, "endDate": None,
                       "folderID": None, "bookmarked": False, "sessionListScope": 2}}
            rv = s.post(f"{list_url}/GetSessions", json=payload, headers=hdrs, timeout=10)
            out["webmethod"] = f"HTTP {rv.status_code} | {rv.text[:500]}"
        except Exception as ex:
            out["webmethod"] = f"ERR: {ex}"

        lines = "\n\n".join(f"{k}:\n  {v}" for k, v in out.items())
        body = f"""
        <h1>Debug</h1>
        <div class="card"><pre style="white-space:pre-wrap;font-size:0.7rem;color:#94a3b8">{lines}</pre></div>
        <a href="/" class="btn btn-primary">Back</a>"""
    except Exception as e:
        body = f'<h1>Debug Error</h1><div class="alert alert-err">{e}</div>'
    return PAGE.format(body=body)


@app.route("/lectures")
def lectures():
    error = request.args.get("error")
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


if __name__ == "__main__":
    app.run(debug=True, port=5000)
