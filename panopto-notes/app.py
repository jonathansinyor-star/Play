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
    """Log in via SSO — tries three independent strategies. Returns requests.Session or None."""
    from bs4 import BeautifulSoup
    from urllib.parse import urljoin

    global _sso_last_error
    username = os.environ.get("MOODLE_USERNAME", "")
    password = os.environ.get("MOODLE_PASSWORD", "")
    moodle_base = os.environ.get("MOODLE_URL", "https://moodle.tau.ac.il").rstrip("/")

    if not username or not password:
        _sso_last_error = "Missing MOODLE_USERNAME or MOODLE_PASSWORD"
        return None

    s = req.Session()
    s.headers["User-Agent"] = UA

    # ── helpers ──────────────────────────────────────────────────────────────

    def _extract_js_url(html, base):
        """Find redirect URL embedded in JavaScript or meta-refresh on the page."""
        for pat in [
            r'location\.href\s*=\s*["\']([^"\']{10,})["\']',
            r'location\.replace\s*\(\s*["\']([^"\']{10,})["\']',
            r'window\.location\s*=\s*["\']([^"\']{10,})["\']',
            r'<meta[^>]+http-equiv=["\']refresh["\'][^>]+content=["\'][^;]+;\s*url=([^"\'>\s]+)',
        ]:
            m = re.search(pat, html, re.IGNORECASE)
            if m:
                u = m.group(1).strip().strip('"\'')
                return u if u.startswith("http") else urljoin(base, u)
        return None

    def _fill_and_post(resp):
        """Fill credentials into the first form found and POST it."""
        soup = BeautifulSoup(resp.text, "html.parser")
        form = soup.find("form")
        if not form:
            return None
        action = form.get("action") or resp.url
        if not action.startswith("http"):
            action = urljoin(resp.url, action)
        data = {i["name"]: i.get("value", "")
                for i in form.find_all("input") if i.get("name")}
        pass_fields = [i["name"] for i in form.find_all("input", {"type": "password"}) if i.get("name")]
        text_fields = [i["name"] for i in form.find_all("input")
                       if i.get("name") and i.get("type", "text").lower() in ("text", "email", "")]
        for k in ("Ecom_User_ID", "username", "loginname", "j_username", "userid", "user"):
            if k in data:
                data[k] = username
                break
        else:
            if text_fields:
                data[text_fields[0]] = username
        for k in ("Ecom_Password", "password", "passwd", "j_password"):
            if k in data:
                data[k] = password
                break
        else:
            if pass_fields:
                data[pass_fields[0]] = password
        try:
            return s.post(action, data=data, allow_redirects=True, timeout=30)
        except Exception:
            return None

    def _follow_relays(resp, depth=10):
        """Follow SAML relay forms, password forms, and JS redirects until .ASPXAUTH appears."""
        r = resp
        for _ in range(depth):
            if any(c.name == ".ASPXAUTH" for c in s.cookies):
                break
            soup = BeautifulSoup(r.text, "html.parser")
            form = soup.find("form")

            # Password form — submit credentials
            if form and form.find("input", {"type": "password"}):
                r2 = _fill_and_post(r)
                if r2:
                    r = r2
                    _sso_last_error += f" | creds→{r.url[:50]}"
                    continue
                break

            # SAML/CAS auto-submit relay form
            if form:
                ra = form.get("action", "")
                rd = {i["name"]: i.get("value", "")
                      for i in form.find_all("input") if i.get("name")}
                relay_keys = ("SAMLResponse", "RelayState", "SAMLRequest",
                              "wresult", "wctx", "lt", "execution", "ticket")
                if ra and any(k in rd for k in relay_keys):
                    if not ra.startswith("http"):
                        ra = urljoin(r.url, ra)
                    try:
                        r = s.post(ra, data=rd, allow_redirects=True, timeout=20)
                        _sso_last_error += f" | relay→{r.url[:50]}"
                        continue
                    except Exception:
                        break

            # JS / meta-refresh redirect
            js_url = _extract_js_url(r.text, r.url)
            if js_url and js_url != r.url:
                try:
                    r = s.get(js_url, allow_redirects=True, timeout=20)
                    _sso_last_error += f" | js→{r.url[:50]}"
                    continue
                except Exception:
                    break
            break
        return r

    def _panopto_force_auth(auth_cas):
        """Trigger Panopto SSO by adding instance= to Login.aspx URL.

        The browser onclick handler does:
            window.location.search += '&instance=Moodle2025'; return false;
        The server returns 200 with JavaScript that reads the instance param
        and builds the Moodle SAML auth URL. We must find that URL in the body.
        """
        try:
            # sandboxCookie proves cookies work (set by Panopto.Login.checkStorageAccess())
            s.cookies.set("sandboxCookie", "1", domain="tau.cloud.panopto.eu")
            s.cookies.set("UserSettings", f"LastLoginMembershipProvider={auth_cas}",
                          domain="tau.cloud.panopto.eu")

            # First visit Login.aspx without instance= to get session cookies
            r0 = s.get(f"{PANOPTO_BASE}/Panopto/Pages/Auth/Login.aspx",
                       params={"authCAS": auth_cas}, allow_redirects=True, timeout=20)
            _sso_last_error_parts.append(f"{auth_cas}→{r0.url[:60]}")
            if any(c.name == ".ASPXAUTH" for c in s.cookies):
                return True

            # Now add instance= to trigger the JS-initiated auth flow
            r = s.get(f"{PANOPTO_BASE}/Panopto/Pages/Auth/Login.aspx",
                      params={"authCAS": auth_cas, "instance": auth_cas},
                      allow_redirects=False, timeout=20)
            loc = r.headers.get("Location", "")
            _sso_last_error_parts.append(f"+inst→{r.status_code} loc={loc[:80]}")

            if r.status_code in (301, 302, 303, 307, 308) and loc:
                if not loc.startswith("http"):
                    loc = urljoin(PANOPTO_BASE, loc)
                r2 = s.get(loc, allow_redirects=True, timeout=20)
                _sso_last_error_parts.append(f"→{r2.url[:70]}")
                if any(c.name == ".ASPXAUTH" for c in s.cookies):
                    return True
                _follow_relays(r2)
            elif r.status_code == 200:
                # JS redirect in body — check with _follow_relays and direct URL search
                _follow_relays(r)
                if not any(c.name == ".ASPXAUTH" for c in s.cookies):
                    # Search body for moodle/auth URLs and navigate to them
                    body_urls = re.findall(
                        r'https?://[^\s"\'<>]*(?:moodle|saml|nidp|sso)[^\s"\'<>]*',
                        r.text, re.I)
                    for burl in body_urls[:3]:
                        try:
                            r_b = s.get(burl, allow_redirects=True, timeout=20)
                            _sso_last_error_parts.append(f"body→{r_b.url[:60]}")
                            _follow_relays(r_b)
                            if any(c.name == ".ASPXAUTH" for c in s.cookies):
                                return True
                        except Exception:
                            pass
        except Exception as e:
            _sso_last_error_parts.append(f"err:{e}")
        return any(c.name == ".ASPXAUTH" for c in s.cookies)

    _sso_last_error_parts = []

    # ── STRATEGY A: Panopto-first (most direct path) ─────────────────────────
    # Panopto Login.aspx with authCAS → ViewState form → POST → Moodle SAML →
    # NetIQ HTML form → creds → NetIQ → Moodle → Panopto (.ASPXAUTH set)
    _sso_last_error_parts.append("A.panopto")
    for auth_cas in ("Moodle2025", "MOODLE", "Moodle"):
        if _panopto_force_auth(auth_cas):
            break
        if any(c.name == ".ASPXAUTH" for c in s.cookies):
            break

    # ── STRATEGY B: Moodle-first with JS redirect parsing ────────────────────
    # Moodle /login/index.php → (JS redirect or form) → NetIQ → creds →
    # SAML chain → Moodle session → Panopto Login.aspx → .ASPXAUTH
    if not any(c.name == ".ASPXAUTH" for c in s.cookies):
        _sso_last_error_parts.append("B.moodle")
        try:
            r = s.get(f"{moodle_base}/login/index.php", allow_redirects=True, timeout=20)
            _sso_last_error_parts.append(f"→{r.url[:60]}")

            # Try HTML form directly
            r2 = _fill_and_post(r)
            if r2:
                _sso_last_error_parts.append(f"form→{r2.url[:50]}")
                _follow_relays(r2)
            else:
                # Parse JS redirect to find the real login page (e.g. nidp.tau.ac.il)
                js_url = _extract_js_url(r.text, r.url)
                if js_url:
                    _sso_last_error_parts.append(f"jsredir→{js_url[:60]}")
                    r3 = s.get(js_url, allow_redirects=True, timeout=20)
                    _sso_last_error_parts.append(f"→{r3.url[:50]}")
                    r4 = _fill_and_post(r3)
                    if r4:
                        _sso_last_error_parts.append(f"form→{r4.url[:50]}")
                        _follow_relays(r4)
                    else:
                        # Try following relay chain from wherever we landed
                        _follow_relays(r3)

            # If we have Moodle session but not Panopto, trigger Panopto SSO
            if not any(c.name == ".ASPXAUTH" for c in s.cookies):
                for auth_cas in ("Moodle2025", "MOODLE", "Moodle"):
                    if _panopto_force_auth(auth_cas):
                        break
        except Exception as e:
            _sso_last_error_parts.append(f"moodle_err:{e}")

    # ── STRATEGY C: Moodle token.php (diagnostic — shows if password works) ──
    if not any(c.name == ".ASPXAUTH" for c in s.cookies):
        _sso_last_error_parts.append("C.token_api")
        try:
            tr = s.post(f"{moodle_base}/login/token.php", data={
                "username": username, "password": password, "service": "moodle_mobile_app"
            }, allow_redirects=True, timeout=15)
            _sso_last_error_parts.append(f"status={tr.status_code} body={tr.text[:120]}")
        except Exception as e:
            _sso_last_error_parts.append(f"err:{e}")

    _sso_last_error = " | ".join(_sso_last_error_parts)

    if any(c.name == ".ASPXAUTH" for c in s.cookies):
        _sso_last_error = "SSO SUCCESS"
        return s

    moodle_cookies = [c.name for c in s.cookies
                      if any(d in (c.domain or "") for d in ("tau.ac.il", "panopto"))]
    _sso_last_error += f" | FAILED cookies={moodle_cookies}"
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


def _extract_js_url_debug(html_text, base):
    """Standalone JS redirect extractor for use outside SSO function."""
    from urllib.parse import urljoin
    for pat in [
        r'location\.href\s*=\s*["\']([^"\']{10,})["\']',
        r'location\.replace\s*\(\s*["\']([^"\']{10,})["\']',
        r'window\.location\s*=\s*["\']([^"\']{10,})["\']',
        r'<meta[^>]+http-equiv=["\']refresh["\'][^>]+content=["\'][^;]+;\s*url=([^"\'>\s]+)',
    ]:
        m = re.search(pat, html_text, re.IGNORECASE)
        if m:
            u = m.group(1).strip().strip('"\'')
            return u if u.startswith("http") else urljoin(base, u)
    return None


@app.route("/debug")
def debug():
    """Auth diagnostics — shows SSO trace, cookies, WebMethod result, and Moodle page source."""
    import html as html_mod
    out = {}
    try:
        reset_session()  # always try a fresh SSO attempt on debug page
        s = get_session()
        csrf = unquote(s.cookies.get("csrfToken", ""))
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

        # Step-by-step Panopto Login.aspx POST trace (fresh session, no cookies)
        # This tells us: does Panopto redirect to Moodle/NetIQ, or loop back?
        moodle_base = os.environ.get("MOODLE_URL", "https://moodle.tau.ac.il").rstrip("/")
        try:
            from bs4 import BeautifulSoup as _BS
            sf = req.Session()
            sf.headers["User-Agent"] = UA
            sf.cookies.set("UserSettings", "LastLoginMembershipProvider=Moodle2025",
                           domain="tau.cloud.panopto.eu")
            # Step 1: GET Login.aspx
            r1 = sf.get(f"{PANOPTO_BASE}/Panopto/Pages/Auth/Login.aspx",
                        params={"authCAS": "Moodle2025"}, allow_redirects=True, timeout=12)
            from urllib.parse import urljoin as _urljoin
            out["pan_step1_url"] = r1.url[:120]
            soup1 = _BS(r1.text, "html.parser")
            form1 = soup1.find("form")
            if form1:
                action1 = form1.get("action") or r1.url
                if not action1.startswith("http"):
                    action1 = _urljoin(r1.url, action1)
                inp_names = {i.get("name"): i.get("value","")[:30]
                             for i in form1.find_all("input") if i.get("name")}
                out["pan_form_field_names"] = str(list(inp_names.keys()))

                # THE KEY: find 'Moodle2025' in the page and show surrounding context
                # This reveals exactly how the JS triggers the redirect
                idx = r1.text.find('Moodle2025')
                if idx >= 0:
                    out["moodle2025_ctx"] = r1.text[max(0, idx - 200):idx + 300]
                else:
                    out["moodle2025_ctx"] = "NOT FOUND IN PAGE HTML"

                # Show full href/onclick of doPostBack links (200 chars, untruncated)
                dopost_links = []
                for a in soup1.find_all("a"):
                    h = a.get("href",""); oc = a.get("onclick","")
                    if "doPostBack" in h or "doPostBack" in oc:
                        dopost_links.append({"href": h[:200], "onclick": oc[:200]})
                out["pan_dopost_links"] = str(dopost_links[:4])
                # Decode &#39; and extract doPostBack args
                decoded_html = r1.text.replace("&#39;","'")
                dopost_raw = re.findall(r"__doPostBack\('([^']+)','([^']*)'\)", decoded_html)
                out["pan_dopostback_decoded"] = str(dopost_raw[:6])

                # Any JSON blobs containing auth provider info
                json_with_auth = [m[:200] for m in re.findall(r'\{[^{}]{20,400}\}', r1.text)
                                  if any(k in m.lower() for k in ('moodle', 'provider', 'authcas'))]
                out["pan_json_auth"] = str(json_with_auth[:3])

                # KEY: GET with instance= + sandboxCookie (mimics what browser does after onclick)
                sf.cookies.set("sandboxCookie", "1", domain="tau.cloud.panopto.eu")
                r_inst = sf.get(f"{PANOPTO_BASE}/Panopto/Pages/Auth/Login.aspx",
                                params={"authCAS": "Moodle2025", "instance": "Moodle2025"},
                                allow_redirects=False, timeout=10)
                out["pan_instance_status"] = r_inst.status_code
                out["pan_instance_location"] = r_inst.headers.get("Location", "(none)")
                # Show body to find the JS-embedded auth URL
                out["pan_instance_body_start"] = r_inst.text[:600]
                # Find moodle/saml/auth URLs embedded in the body
                inst_urls = re.findall(
                    r'https?://[^\s"\'\\<>]*(?:moodle|saml|nidp|sso|auth)[^\s"\'\\<>]*',
                    r_inst.text, re.I)
                out["pan_instance_auth_urls"] = str(inst_urls[:6])
                # Check for any JS redirect patterns in the body
                out["pan_instance_js_redir"] = str(_extract_js_url_debug(r_inst.text, r_inst.url))
                # Show any JSON blobs mentioning providers
                inst_json = [m[:200] for m in re.findall(r'\{[^{}]{20,300}\}', r_inst.text)
                             if any(k in m.lower() for k in ('moodle','provider','redirect','saml','url'))]
                out["pan_instance_json"] = str(inst_json[:3])
            else:
                out["pan_step1_form"] = "NO FORM FOUND"
                out["pan_step1_js_url"] = str(_extract_js_url_debug(r1.text, r1.url))
        except Exception as ex:
            out["pan_trace_err"] = str(ex)

        # Test WebMethod with decoded CSRF token
        try:
            payload = {"queryParameters": {"query": "", "sortColumn": 1, "sortAscending": False,
                       "maxResults": 5, "page": 0, "startDate": None, "endDate": None,
                       "folderID": None, "bookmarked": False, "sessionListScope": 2}}
            rv = s.post(f"{list_url}/GetSessions", json=payload, headers=hdrs, timeout=10)
            out["webmethod"] = f"HTTP {rv.status_code} | {rv.text[:400]}"
        except Exception as ex:
            out["webmethod"] = f"ERR: {ex}"

        lines = "\n\n".join(f"{k}:\n  {html_mod.escape(str(v))}" for k, v in out.items())
        body = f"""
        <h1>Debug</h1>
        <div class="card"><pre style="white-space:pre-wrap;font-size:0.7rem;color:#94a3b8">{lines}</pre></div>
        <a href="/lectures" class="btn btn-primary">Lectures</a>
        &nbsp;<a href="/set-cookie" class="btn btn-sm" style="color:#94a3b8">Paste Cookies</a>"""
    except Exception as e:
        body = f'<h1>Debug Error</h1><div class="alert alert-err">{html.escape(str(e))}</div>'
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
