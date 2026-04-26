import os
import re
import json
import asyncio
import subprocess
import tempfile
import math
import time
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
# Flag so auto-resume and manual Generate All don't both start at once
_run_all_lock = _threading.Lock()

PANOPTO_BASE = "https://tau.cloud.panopto.eu"
SINCE_DATE = "2026-03-01T00:00:00.000Z"
# Use Railway persistent volume at /data if available; else /tmp (lost on redeploy)
NOTES_DIR = "/data" if os.path.isdir("/data") else tempfile.gettempdir()
NOTES_PERSISTENT = os.path.isdir("/data")
# Saved to /data so Generate All auto-resumes after a container restart
_PENDING_ALL_PATH = os.path.join(NOTES_DIR, "pending_all.json")

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
            all_raw = []
            page = 0
            while True:
                paged = {**payload, "queryParameters": {**payload["queryParameters"], "page": page}}
                r = s.post(endpoint, json=paged, headers=hdrs, timeout=30)
                if r.status_code != 200:
                    if page == 0:
                        last_err = f"HTTP {r.status_code}: {r.text[:200]}"
                    break
                batch = r.json().get("d", {}).get("Results", [])
                all_raw.extend(batch)
                if len(batch) < max_results:
                    break  # last page
                page += 1
            if all_raw:
                return _parse_webmethod_results(all_raw), f"webmethod {len(all_raw)} sessions"
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


# Module-level cache: session_id -> session dict (populated by list_shared_sessions)
_sessions_cache = {}

_SESSIONS_LIST_CACHE = os.path.join(tempfile.gettempdir(), "sessions_list.json")
_SESSIONS_LIST_TTL = 900  # 15 minutes — reload only when user clicks Refresh


def list_shared_sessions(force_refresh=False):
    s = get_session()
    since = datetime(2026, 3, 1)

    def _filter_since(items):
        out = []
        for item in items:
            start = item.get("StartTime", "")
            try:
                dt = datetime.fromisoformat(start.replace("Z", "").replace("+00:00", ""))
                if dt >= since:
                    out.append(item)
            except Exception:
                out.append(item)
        return out

    def _cache_and_filter(items, save=False):
        for item in items:
            if item.get("Id"):
                _sessions_cache[item["Id"]] = item
        if save and items:
            try:
                with open(_SESSIONS_LIST_CACHE, "w") as f:
                    json.dump({"ts": time.time(), "items": items}, f)
            except Exception:
                pass
        return _filter_since(items)

    # Serve from file cache if fresh enough (avoids 20-30s Playwright run every load)
    if not force_refresh:
        try:
            with open(_SESSIONS_LIST_CACHE) as f:
                cached = json.load(f)
            if time.time() - cached.get("ts", 0) < _SESSIONS_LIST_TTL:
                return _cache_and_filter(cached.get("items", []))
        except Exception:
            pass

    # 1. Internal WebMethod (tries multiple payload shapes)
    try:
        results, _method = _webmethod_sessions(s)
        return _cache_and_filter(results, save=True)
    except Exception:
        pass

    # 2. REST API — try multiple version paths, paginating through all results
    for api_path in [
        "/Panopto/api/v1/sessions",
        "/Panopto/api/v1.0/sessions",
        "/Panopto/api/sessions",
    ]:
        try:
            all_results, offset = [], 0
            while True:
                r = s.get(
                    f"{PANOPTO_BASE}{api_path}",
                    params={"isSharedWithMe": "true", "sortField": "StartTime",
                            "sortOrder": "Desc", "pagination[maxResults]": 100,
                            "pagination[index]": offset},
                    timeout=30,
                )
                if not r.ok:
                    break
                data = r.json()
                batch = data.get("Results", [])
                all_results.extend(batch)
                total = data.get("TotalResultsCount", 0)
                if len(batch) < 100 or (total and len(all_results) >= total):
                    break
                offset += 100
            if all_results:
                return _cache_and_filter(all_results, save=True)
        except Exception:
            pass

    # 3. Scrape HTML of List.aspx
    try:
        results, _method = _scrape_list_aspx(s)
        return _cache_and_filter(results, save=True)
    except Exception:
        pass

    # 4. Use a real browser — intercept whichever API the Panopto JS actually calls
    results = _playwright_list_sessions(s)
    return _cache_and_filter(results, save=True)


def _get_podcast_url(s, session_id):
    """Return a direct downloadable audio URL from Panopto's Podcast endpoint.
    These are real files (not HLS manifests) and work with session cookies.
    Returns None if none of the known paths respond with 200."""
    for path in [
        f"/Panopto/Podcast/Cast.svc/MP3/{session_id}",
        f"/Panopto/Podcast/Cast.svc/MP3?id={session_id}&isMp3=true",
        f"/Panopto/Podcast/{session_id}/podcast.mp4",
        f"/Panopto/Podcast/{session_id}/podcast.mp3",
    ]:
        try:
            r = s.head(f"{PANOPTO_BASE}{path}", allow_redirects=True, timeout=10)
            if r.ok and "text/html" not in r.headers.get("content-type", ""):
                return f"{PANOPTO_BASE}{path}"
        except Exception:
            pass
    return None


def get_session_detail(session_id):
    s = get_session()
    cached = _sessions_cache.get(session_id, {})

    # 1. Always try Podcast endpoint first — it returns a real downloadable file,
    #    not an HLS manifest. HLS streams from DeliveryInfo use CDN-signed URLs
    #    that expire and can't be easily authenticated in ffmpeg.
    podcast_url = _get_podcast_url(s, session_id)

    # 2. DeliveryInfo.aspx — mainly for caption URL and metadata.
    #    We use the HLS stream URL only as a last resort if no podcast URL found.
    hls_url = None
    caption_uri = None
    for params in [
        {"deliveryId": session_id, "getCaptions": "true", "responseType": "json"},
        {"deliveryId": session_id, "responseType": "json"},
        {"deliveryId": session_id},
    ]:
        try:
            r = s.get(
                f"{PANOPTO_BASE}/Panopto/Pages/Viewer/DeliveryInfo.aspx",
                params=params, timeout=20,
            )
            if not r.ok:
                continue
            if "html" in r.headers.get("content-type", ""):
                break
            data = r.json()
            delivery = data.get("Delivery") or data
            caption_uri = (delivery.get("CaptionDownloadUri")
                           or delivery.get("CaptionsUri")
                           or delivery.get("CaptionUri"))
            if not podcast_url:
                for stream in (delivery.get("Streams", []) or []):
                    url = stream.get("StreamHttpUrl") or stream.get("StreamUrl", "")
                    if url:
                        hls_url = url
                        break
            return {
                "Id": session_id,
                "Name": cached.get("Name") or delivery.get("Name", "Untitled"),
                "StartTime": cached.get("StartTime") or delivery.get("StartTime", ""),
                "Duration": cached.get("Duration") or delivery.get("Duration"),
                "DownloadUrl": podcast_url or hls_url,
                "CaptionDownloadUrl": caption_uri,
                "_detail_source": "DeliveryInfo",
                "_url_type": "podcast" if podcast_url else ("hls" if hls_url else "none"),
            }
        except Exception:
            continue

    # 3. Fall back to cache; inject podcast URL if we found one
    if cached or podcast_url:
        result = dict(cached)
        if podcast_url:
            result["DownloadUrl"] = podcast_url
            result["_url_type"] = "podcast"
        return result

    return {}


# ---------------------------------------------------------------------------
# Transcript extraction
# ---------------------------------------------------------------------------

def _playwright_get_media_urls(session_id):
    """Open the Panopto viewer in a real browser and extract:
    1. Captions/transcript text (preferred — instant, no quota usage)
    2. CDN-signed HLS manifest URL as fallback for audio transcription

    Strategy for captions:
    - Intercept any network response that looks like caption/transcript data
    - Actively click the Transcript panel button to trigger caption loading
    - Scrape the visible transcript text from the DOM

    Returns (caption_text_or_None, m3u8_url_or_None).
    """
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

    s = get_session()
    pw_cookies = [{"name": c.name, "value": c.value,
                   "domain": "tau.cloud.panopto.eu", "path": "/"}
                  for c in s.cookies]

    caption_text = None
    m3u8_url = None
    fetched_caption_urls = []  # log for debugging

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

        def on_request(req):
            nonlocal m3u8_url
            url = req.url
            if ".m3u8" in url and not m3u8_url:
                m3u8_url = url

        def on_response(resp):
            nonlocal caption_text
            if resp.status != 200 or caption_text:
                return
            url = resp.url.lower()
            ct = resp.headers.get("content-type", "").lower()
            # Broad catch: any text response that looks like captions
            is_caption_url = any(k in url for k in [
                "caption", "srt", "vtt", "transcript",
                "generatesrt", "generatevtt", "captions",
            ])
            is_text_vtt = "text/vtt" in ct or "text/plain" in ct
            if is_caption_url or is_text_vtt:
                fetched_caption_urls.append(resp.url[:120])
                try:
                    text = resp.text()
                    if len(text) > 100:
                        caption_text = text
                except Exception:
                    pass

        page.on("request", on_request)
        page.on("response", on_response)

        try:
            page.goto(
                f"{PANOPTO_BASE}/Panopto/Pages/Viewer.aspx?id={session_id}",
                wait_until="networkidle", timeout=35000,
            )
            page.wait_for_timeout(3000)

            # Actively open the Transcript panel to trigger caption loading
            if not caption_text:
                for btn_sel in [
                    "button[title*='Transcript' i]",
                    "button[aria-label*='transcript' i]",
                    "[data-control='transcript']",
                    ".viewer-nav-tab-transcript",
                    "li[data-tab='transcript'] button",
                    "button:has-text('Transcript')",
                ]:
                    try:
                        btn = page.locator(btn_sel).first
                        if btn.is_visible(timeout=2000):
                            btn.click()
                            page.wait_for_timeout(3000)
                            break
                    except Exception:
                        pass

            # Scrape transcript text directly from the DOM
            if not caption_text:
                for sel in [
                    ".event-transcript-item",
                    ".transcript-wrapper .transcript-line",
                    ".viewer-transcript-wrapper",
                    "[class*='transcript'] [class*='text']",
                    "[class*='caption'] span",
                ]:
                    try:
                        items = page.locator(sel).all_inner_texts()
                        joined = " ".join(t.strip() for t in items if t.strip())
                        if len(joined) > 200:
                            caption_text = joined
                            break
                    except Exception:
                        pass

        except Exception:
            pass

        browser.close()

    return caption_text, m3u8_url



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


def download_and_transcribe(session_id, download_url, caption_url, status_cb=None):
    """Return (transcript_text, method_used)."""
    s = get_session()

    def _try_caption(url):
        try:
            r = s.get(url, timeout=20)
            if r.ok and len(r.text) > 100:
                return strip_srt_timestamps(r.text)
        except Exception:
            pass
        return None

    # Try captions first — instant and free
    if caption_url:
        text = _try_caption(caption_url)
        if text:
            return text, "captions"

    # Try Panopto's well-known caption/transcript endpoints (work on all versions)
    for cap_url in [
        f"{PANOPTO_BASE}/Panopto/Pages/Transcription/GenerateSRT.ashx?id={session_id}",
        f"{PANOPTO_BASE}/Panopto/Pages/Transcription/GenerateVTT.ashx?id={session_id}",
        f"{PANOPTO_BASE}/Panopto/Podcast/Cast.svc/caption/{session_id}.srt",
    ]:
        text = _try_caption(cap_url)
        if text:
            return text, "captions"

    # Use Playwright to load the viewer and intercept captions + CDN stream URL.
    # The CDN stream URL has the auth token embedded in the URL itself, so ffmpeg
    # can download it without any cookie authentication.
    try:
        cap_text, m3u8_url = _playwright_get_media_urls(session_id)
        if cap_text:
            return strip_srt_timestamps(cap_text), "captions_playwright"
        if m3u8_url:
            # Replace the download_url with the CDN-signed m3u8 URL
            download_url = m3u8_url
    except Exception:
        pass

    if not download_url:
        return None, "no_source"

    groq_client = Groq(api_key=os.environ["GROQ_API_KEY"])

    # All intermediate files go to NOTES_DIR (/data if volume mounted, else /tmp).
    # This means partial transcripts and cached audio survive Railway restarts,
    # so a crash mid-lecture picks up exactly where it stopped.
    partial_path = os.path.join(NOTES_DIR, f"partial_{session_id}.json")

    def _load_partial():
        try:
            with open(partial_path) as f:
                return json.load(f)
        except Exception:
            return {"chunks_done": 0, "parts": []}

    def _save_partial(chunks_done, parts):
        with open(partial_path, "w") as f:
            json.dump({"chunks_done": chunks_done, "parts": parts}, f)

    def _clear_partial():
        try:
            os.unlink(partial_path)
        except Exception:
            pass

    # Cached audio also goes to NOTES_DIR so it survives a container restart.
    # Deleted after successful transcription to free up space.
    cached_mp3 = os.path.join(NOTES_DIR, f"audio_{session_id}.mp3")
    need_convert = not os.path.exists(cached_mp3) or os.path.getsize(cached_mp3) < 50_000

    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f:
        tmp_src = f.name

    try:
        if need_convert:
            # Download audio
            with s.get(download_url, stream=True, timeout=300, allow_redirects=True) as r:
                r.raise_for_status()
                with open(tmp_src, "wb") as out:
                    for chunk in r.iter_content(chunk_size=1024 * 1024):
                        out.write(chunk)

            src_size = os.path.getsize(tmp_src)
            if src_size < 50_000:
                if ".m3u8" in download_url or src_size < 5_000:
                    subprocess.run(
                        ["ffmpeg", "-y", "-i", download_url,
                         "-vn", "-ar", "16000", "-ac", "1", "-ab", "32k", cached_mp3],
                        check=True, capture_output=True, timeout=600,
                    )
                else:
                    return None, f"download_too_small ({src_size} bytes)"
            else:
                subprocess.run(
                    ["ffmpeg", "-y", "-i", tmp_src,
                     "-vn", "-ar", "16000", "-ac", "1", "-ab", "32k", cached_mp3],
                    check=True, capture_output=True, timeout=300,
                )

        # Deepgram path — no chunking, no rate limits, uses free $200 credit
        deepgram_key = os.environ.get("DEEPGRAM_API_KEY", "")
        if deepgram_key:
            try:
                if status_cb:
                    status_cb("Transcribing with Deepgram (no rate limits)…")
                with open(cached_mp3, "rb") as af:
                    resp = requests.post(
                        "https://api.deepgram.com/v1/listen",
                        params={"model": "nova-2", "smart_format": "true"},
                        headers={"Authorization": f"Token {deepgram_key}",
                                 "Content-Type": "audio/mp3"},
                        data=af,
                        timeout=600,
                    )
                resp.raise_for_status()
                dg_transcript = (resp.json()["results"]["channels"][0]
                                 ["alternatives"][0]["transcript"])
                if dg_transcript.strip():
                    _clear_partial()
                    try:
                        os.unlink(cached_mp3)
                    except Exception:
                        pass
                    return dg_transcript, "deepgram"
            except Exception:
                pass  # fall through to Groq Whisper

        file_size = os.path.getsize(cached_mp3)
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", cached_mp3],
            capture_output=True, text=True, check=True,
        )
        total_seconds = float(probe.stdout.strip())

        max_bytes = 20 * 1024 * 1024
        num_chunks = max(1, math.ceil(file_size / max_bytes))
        chunk_seconds = math.ceil(total_seconds / num_chunks)

        # Resume from saved partial transcript if available
        partial = _load_partial()
        start_chunk = partial["chunks_done"]
        transcript_parts = partial["parts"]

        for i in range(start_chunk, num_chunks):
            start = i * chunk_seconds
            with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as cf:
                chunk_path = cf.name
            subprocess.run(
                ["ffmpeg", "-y", "-i", cached_mp3, "-ss", str(start),
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
                _save_partial(i + 1, transcript_parts)  # save progress after each chunk
            except Exception as groq_err:
                err_str = str(groq_err)
                if "rate_limit_exceeded" in err_str or "429" in err_str:
                    _save_partial(i, transcript_parts)  # keep what we have so far
                    wait_match = re.search(r"try again in (\d+m\d+s|\d+s)", err_str)
                    wait_str = wait_match.group(1) if wait_match else "~15 minutes"
                    done_chunks = i
                    raise RuntimeError(
                        f"RATE_LIMIT: Groq free tier: processed {done_chunks}/{num_chunks} chunks. "
                        f"Wait {wait_str}, then tap Retry — "
                        f"will resume from chunk {done_chunks} (no re-download needed)."
                    )
                raise
            finally:
                try:
                    os.unlink(chunk_path)
                except Exception:
                    pass

        _clear_partial()  # all done — remove the resume checkpoint
        # Delete cached audio now that transcription is complete — frees space on /data
        try:
            os.unlink(cached_mp3)
        except Exception:
            pass
        return " ".join(transcript_parts), "whisper"

    finally:
        try:
            os.unlink(tmp_src)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Notes generation
# ---------------------------------------------------------------------------

NOTES_PROMPT = """You are an expert academic tutor creating the most comprehensive exam-preparation notes possible from a university lecture.

Lecture: {title}
Date: {date}

Transcript:
{transcript}

Create EXHAUSTIVE exam-focused notes. Include EVERY specific fact, number, name, formula, mechanism, and example from the transcript. Never write "was discussed" — state the actual content. A student must be able to ace an exam using ONLY these notes.

Output the following sections in order:

# {title}
**Date:** {date}
**Lecturer:** [Extract lecturer name from transcript if mentioned; otherwise write "Not stated"]

## What This Lecture Covers
[2–3 sentences on the exact scope and its role in the course]

## Every Point in This Lecture — Full Explanations
Cover EVERY distinct point, argument, and idea the lecturer made, in the order they were presented. For each one use a ### heading and explain it thoroughly:
- WHAT the point is, stated precisely
- WHY the lecturer made it — the reasoning or evidence behind it
- HOW it works if it involves a process, mechanism, or sequence (step by step)
- Specific facts, numbers, formulas, thresholds, names, and units involved
- Any examples or case studies the lecturer used to illustrate it (with full detail)
- How it connects to or builds on other points in the lecture
- Any exceptions, edge cases, or nuances flagged

Do NOT group points vaguely under broad topics. Give every argument, sub-argument, and claim its own ### heading and deep treatment. If the lecturer spent 10 minutes on a concept, your notes for that concept should reflect that depth. Aim for at least 10 bullet points per major point.

## Definitions & Key Terms
[EVERY term introduced. Format: **Term**: full definition plus context — when it applies, what distinguishes it from similar terms]

## Specific Facts for the Exam
[Concrete testable items:
- Exact numbers, percentages, thresholds, dates, quantities
- Named laws, rules, theorems, effects, criteria, classifications
- Precise cause → effect relationships
- Exceptions and special cases the lecturer emphasised]

## Homework & Tasks
[Any assignments, problem sets, readings, submissions, or deadlines mentioned. Be specific about what is required and when. If none mentioned: "None mentioned in this lecture."]

## What to Study Next
- Concepts mentioned but not fully explained — these need independent study
- Prerequisite knowledge assumed by the lecturer — review if unclear
- Topics that naturally follow from this material
- Any textbooks, papers, chapters, or resources the lecturer mentioned (with full names)

## NotebookLM Audio Overview — Ready to Use
Upload this notes file to NotebookLM (notebooklm.google.com) as a source, then click "Audio Overview" and paste this prompt:

"Create a podcast-style discussion of {title}. Cover the following in depth: [list the 3 most important concepts from this lecture]. Walk through the mechanism of [the most important process step by step]. Discuss what a student must know for an exam, including specific facts and numbers. End with a summary of the key takeaways."

For the Guide panel in NotebookLM, add these questions:
[List 6–8 specific questions based on this lecture's content that would make the Audio Overview more focused and useful]

## Key Takeaways — Ranked by Exam Importance
[8–10 most important points from this lecture, ordered from most to least likely to appear on an exam. Be specific.]"""

CHUNK_PROMPT = """You are extracting deep study notes from a university lecture section. Your goal is comprehensive coverage of every point made — not a surface summary.

Lecture section transcript:
{chunk}

For EVERY distinct point, argument, or idea the lecturer makes:
- State the point precisely
- Explain the reasoning or evidence behind it
- If it involves a process or mechanism: list every step in order
- Include ALL specific facts: every number, formula, threshold, date, measurement, percentage
- Include ALL examples used, with every detail given
- Note any exceptions, nuances, or "important" flags the lecturer mentioned

Also capture:
- Every term defined, with its full definition
- Homework, tasks, assignments, or deadlines mentioned
- Lecturer's name if stated

Use bullet points grouped by topic. Do NOT summarise vaguely — preserve specific details. If the lecturer spent a long time on something, your notes should reflect that."""


def generate_notes(title, date, transcript, status_cb=None):
    groq_client = Groq(api_key=os.environ["GROQ_API_KEY"])

    def _call(messages, max_tokens):
        """Call Groq LLaMA with automatic backoff on rate-limit errors."""
        for attempt in range(5):
            try:
                return groq_client.chat.completions.create(
                    model="llama-3.3-70b-versatile",
                    messages=messages,
                    temperature=0.3,
                    max_tokens=max_tokens,
                ).choices[0].message.content
            except Exception as e:
                err = str(e)
                if "429" in err or "rate_limit" in err or "413" in err:
                    m = re.search(r"try again in (\d+\.?\d*)s", err)
                    wait = float(m.group(1)) + 2 if m else min(60 * (attempt + 1), 120)
                    if status_cb:
                        status_cb(f"AI rate limit — waiting {int(wait)}s then continuing…")
                    time.sleep(wait)
                    continue
                raise
        raise RuntimeError("Groq API failed after 5 retries")

    words = transcript.split()
    # Groq free tier: 12,000 TPM = input_tokens + max_output_tokens per minute.
    # New detailed NOTES_PROMPT ≈ 800 tokens overhead.
    # DIRECT path: 5000 words × 1.2 ≈ 6000 tokens + 800 prompt + 3500 output = 10,300 TPM ✓
    # CHUNK path: 5000 words × 1.2 ≈ 6000 tokens + 200 chunk-prompt + 1000 output = 7,200 TPM ✓
    # Synthesis: 6 chunks × 1000 tokens = 6000 + 800 prompt + 4096 output = 10,896 TPM ✓
    DIRECT_LIMIT = 5000
    CHUNK_WORDS = 5000

    if len(words) <= DIRECT_LIMIT:
        prompt = NOTES_PROMPT.format(title=title, date=date, transcript=transcript)
        return _call([{"role": "user", "content": prompt}], max_tokens=3500)

    # Long transcript: summarise each 5000-word chunk, then synthesise into full notes.
    chunks = [" ".join(words[i:i + CHUNK_WORDS]) for i in range(0, len(words), CHUNK_WORDS)]
    summaries = []
    for idx, chunk in enumerate(chunks, 1):
        if status_cb:
            status_cb(f"Extracting detail — section {idx}/{len(chunks)}…")
        try:
            text = _call(
                [{"role": "user", "content": CHUNK_PROMPT.format(chunk=chunk)}],
                max_tokens=1000,
            )
            summaries.append(f"[Section {idx}/{len(chunks)}]\n{text}")
        except Exception as e:
            summaries.append(f"[Section {idx}/{len(chunks)} — error: {e}]")

    if status_cb:
        status_cb("Synthesising all sections into final notes…")

    combined = "\n\n".join(summaries)
    final_prompt = NOTES_PROMPT.format(title=title, date=date, transcript=combined)
    return _call([{"role": "user", "content": final_prompt}], max_tokens=4096)


def notes_path(session_id):
    return os.path.join(NOTES_DIR, f"notes_{session_id}.md")


# ---------------------------------------------------------------------------
# Course classification, meta storage, and audio explainer
# ---------------------------------------------------------------------------

CLASSIFY_PROMPT = """You are organising university lecture notes into courses.

Given these lecture notes, extract:
1. COURSE: The subject/course name (e.g. "Business Law", "Microeconomics"). Use the exact same name for related lectures.
2. LECTURER: The lecturer's name (e.g. "Dr. Cohen"). Write "Unknown" if not stated.
3. TASKS: Any assignments, readings, submissions, or deadlines mentioned. Each as a complete sentence.

Return ONLY valid JSON, no other text:
{{"course": "...", "lecturer": "...", "tasks": ["..."]}}

Title: {title}
Date: {date}

Notes excerpt:
{notes_excerpt}"""

EXPLAINER_SCRIPT_PROMPT = """Write a clear 900-word spoken audio guide for a student reviewing this lecture.

Rules:
- Plain spoken English only — no markdown, no headers, no bullet symbols, no asterisks
- Speak directly to the student: "In this lecture you covered..." / "The key idea here is..."
- Explain every major concept clearly, with the examples the lecturer used
- Include specific facts, numbers, and named concepts from the lecture
- End with: "The three things you must remember from this lecture are: one... two... three..."
- Write as natural speech — this will be read aloud by text-to-speech

Lecture: {title}
Date: {date}

Notes:
{notes_excerpt}"""


def _meta_path(session_id):
    return os.path.join(NOTES_DIR, f"meta_{session_id}.json")


def _load_meta(session_id):
    try:
        with open(_meta_path(session_id)) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_meta(session_id, **kwargs):
    path = _meta_path(session_id)
    try:
        with open(path) as f:
            existing = json.load(f)
    except Exception:
        existing = {}
    existing.update(kwargs)
    existing["session_id"] = session_id
    with open(path, "w") as f:
        json.dump(existing, f)


def get_all_lecture_meta():
    """All processed lecture meta dicts, newest first."""
    metas = []
    try:
        for fname in os.listdir(NOTES_DIR):
            if not (fname.startswith("notes_") and fname.endswith(".md")):
                continue
            sid = fname[6:-3]
            meta = _load_meta(sid)
            meta.setdefault("session_id", sid)
            meta.setdefault("title", "Untitled")
            meta.setdefault("date", "")
            meta.setdefault("course", "Uncategorised")
            meta.setdefault("lecturer", "Unknown")
            meta.setdefault("tasks", [])
            meta["has_video"] = os.path.exists(
                os.path.join(NOTES_DIR, f"video_{sid}.mp4"))
            meta["has_explainer"] = os.path.exists(
                os.path.join(NOTES_DIR, f"explainer_{sid}.mp3"))
            metas.append(meta)
    except Exception:
        pass
    return sorted(metas, key=lambda m: m.get("date", ""), reverse=True)


def _course_slug(course_name):
    return re.sub(r"[^a-z0-9]+", "-", course_name.lower()).strip("-") or "uncategorised"


def classify_lecture(session_id, title, date, notes_md, groq_client=None):
    """Ask LLaMA to classify lecture into a course and extract tasks. Saves to meta."""
    if groq_client is None:
        groq_client = Groq(api_key=os.environ["GROQ_API_KEY"])
    try:
        resp = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": CLASSIFY_PROMPT.format(
                title=title, date=date, notes_excerpt=notes_md[:3000])}],
            temperature=0.1,
            max_tokens=300,
        ).choices[0].message.content
        m = re.search(r"\{.*\}", resp, re.DOTALL)
        if m:
            data = json.loads(m.group())
            _save_meta(session_id, title=title, date=date,
                       course=data.get("course", "Uncategorised"),
                       lecturer=data.get("lecturer", "Unknown"),
                       tasks=data.get("tasks", []))
            return
    except Exception:
        pass
    _save_meta(session_id, title=title, date=date)


def generate_explainer_audio(session_id, title, date, notes_md, status_cb=None, groq_client=None):
    """Generate a podcast-style MP3 for a lecture using LLaMA script + edge-tts."""
    audio_path = os.path.join(NOTES_DIR, f"explainer_{session_id}.mp3")
    if os.path.exists(audio_path):
        return
    if groq_client is None:
        groq_client = Groq(api_key=os.environ["GROQ_API_KEY"])
    try:
        if status_cb:
            status_cb(f"Writing audio script for '{title[:35]}'…")
        script = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": EXPLAINER_SCRIPT_PROMPT.format(
                title=title, date=date, notes_excerpt=notes_md[:4500])}],
            temperature=0.4,
            max_tokens=1200,
        ).choices[0].message.content

        if status_cb:
            status_cb("Converting script to audio (edge-tts)…")
        try:
            import edge_tts

            async def _speak():
                comm = edge_tts.Communicate(script, "en-US-AriaNeural")
                await comm.save(audio_path)

            asyncio.run(_speak())
        except Exception:
            # Fallback: gTTS
            from gtts import gTTS
            gTTS(text=script, lang="en").save(audio_path)
    except Exception:
        pass  # explainer is optional — never blocks notes pipeline


SLIDES_PROMPT = """Create a slide deck for this university lecture as JSON.

Generate 10-14 slides. Each slide covers one key concept.
First slide: introduce the topic. Last slide: exam takeaways.

Return ONLY valid JSON, no other text:
{{"slides": [
  {{
    "title": "Slide title (short, clear)",
    "bullets": ["Key point 1", "Key point 2", "Key point 3"],
    "narration": "Spoken explanation, 50-80 words, natural conversational English, no bullet symbols"
  }}
]}}

Lecture: {title}
Date: {date}

Notes:
{notes_excerpt}"""


def _find_font(size):
    """Return a PIL ImageFont, trying system fonts before falling back to default."""
    from PIL import ImageFont
    candidates = [
        "/run/current-system/sw/share/X11/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/nix/var/nix/profiles/default/share/fonts/truetype/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
    ]
    # Ask fontconfig for whatever's available
    try:
        out = subprocess.run(["fc-list", "--format=%{file}\n"],
                             capture_output=True, text=True, timeout=5).stdout
        for line in out.splitlines():
            p = line.strip()
            if p.lower().endswith(".ttf") and os.path.exists(p):
                candidates.insert(0, p)
                break
    except Exception:
        pass
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            pass
    return ImageFont.load_default()


def _render_slide(title, bullets, slide_num, total, lecture_title, date_str, out_path):
    """Render a 1280×720 slide PNG."""
    from PIL import Image, ImageDraw
    W, H = 1280, 720
    img = Image.new("RGB", (W, H), (15, 23, 42))
    d = ImageDraw.Draw(img)

    # Top accent bar
    d.rectangle([(0, 0), (W, 7)], fill=(99, 102, 241))

    # Header: course + slide number
    fn_sm = _find_font(22)
    d.text((40, 20), f"{lecture_title[:55]}  ·  {date_str}", font=fn_sm, fill=(100, 116, 139))
    d.text((W - 80, 20), f"{slide_num}/{total}", font=fn_sm, fill=(100, 116, 139))

    # Title (word-wrapped, up to 2 lines)
    fn_title = _find_font(54)
    words, lines, cur = title.split(), [], []
    for w in words:
        test = " ".join(cur + [w])
        if d.textlength(test, font=fn_title) > W - 80 and cur:
            lines.append(" ".join(cur)); cur = [w]
        else:
            cur.append(w)
    if cur:
        lines.append(" ".join(cur))
    y = 75
    for line in lines[:2]:
        d.text((40, y), line, font=fn_title, fill=(248, 250, 252))
        y += 68

    # Accent underline
    d.rectangle([(40, y + 8), (160, y + 12)], fill=(99, 102, 241))
    y += 36

    # Bullets
    fn_b = _find_font(34)
    for bullet in bullets[:5]:
        d.ellipse([(40, y + 13), (54, y + 27)], fill=(99, 102, 241))
        # wrap bullet
        bwords, blines, bcur = bullet.split(), [], []
        for bw in bwords:
            test = " ".join(bcur + [bw])
            if d.textlength(test, font=fn_b) > W - 120 and bcur:
                blines.append(" ".join(bcur)); bcur = [bw]
            else:
                bcur.append(bw)
        if bcur:
            blines.append(" ".join(bcur))
        for bl in blines[:2]:
            d.text((68, y), bl, font=fn_b, fill=(203, 213, 225))
            y += 46
        y += 6
        if y > H - 50:
            break

    img.save(out_path, "PNG")


def generate_lecture_video(session_id, title, date, notes_md, status_cb=None, groq_client=None):
    """Generate a slide-based MP4 video for a lecture (stored in NOTES_DIR)."""
    video_path = os.path.join(NOTES_DIR, f"video_{session_id}.mp4")
    if os.path.exists(video_path):
        return
    if groq_client is None:
        groq_client = Groq(api_key=os.environ["GROQ_API_KEY"])

    tmp_dir = tempfile.mkdtemp(prefix=f"vid_{session_id[:8]}_")
    try:
        # 1. Generate slide structure
        if status_cb:
            status_cb(f"Planning video for '{title[:30]}'…")
        raw = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": SLIDES_PROMPT.format(
                title=title, date=date, notes_excerpt=notes_md[:5500])}],
            temperature=0.3, max_tokens=2500,
        ).choices[0].message.content
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if not m:
            return
        slides = json.loads(m.group()).get("slides", [])
        if not slides:
            return

        # 2. Per-slide: render PNG + TTS → combine into clip
        clip_paths = []
        for i, slide in enumerate(slides, 1):
            if status_cb:
                status_cb(f"Video slide {i}/{len(slides)}: '{title[:25]}'…")
            png = os.path.join(tmp_dir, f"s{i:03d}.png")
            mp3 = os.path.join(tmp_dir, f"a{i:03d}.mp3")
            mp4 = os.path.join(tmp_dir, f"c{i:03d}.mp4")

            _render_slide(slide.get("title", ""), slide.get("bullets", []),
                          i, len(slides), title, date, png)

            narration = slide.get("narration") or slide.get("title", "")
            try:
                import edge_tts
                async def _tts(txt, dst):
                    await edge_tts.Communicate(txt, "en-US-AriaNeural").save(dst)
                asyncio.run(_tts(narration, mp3))
            except Exception:
                try:
                    from gtts import gTTS
                    gTTS(text=narration, lang="en").save(mp3)
                except Exception:
                    subprocess.run(
                        ["ffmpeg", "-y", "-f", "lavfi",
                         "-i", "anullsrc=r=22050:cl=mono", "-t", "4", mp3],
                        capture_output=True)

            subprocess.run(
                ["ffmpeg", "-y", "-loop", "1", "-i", png, "-i", mp3,
                 "-c:v", "libx264", "-preset", "fast", "-crf", "28",
                 "-c:a", "aac", "-b:a", "96k",
                 "-shortest", "-pix_fmt", "yuv420p", mp4],
                capture_output=True, timeout=120)

            if os.path.exists(mp4) and os.path.getsize(mp4) > 2000:
                clip_paths.append(mp4)

        if not clip_paths:
            return

        # 3. Concatenate all clips
        if status_cb:
            status_cb(f"Assembling video for '{title[:30]}'…")
        concat = os.path.join(tmp_dir, "list.txt")
        with open(concat, "w") as cf:
            for cp in clip_paths:
                cf.write(f"file '{cp}'\n")
        subprocess.run(
            ["ffmpeg", "-y", "-f", "concat", "-safe", "0",
             "-i", concat, "-c", "copy", video_path],
            capture_output=True, timeout=300)

        if status_cb and os.path.exists(video_path):
            status_cb(f"Video ready: '{title[:30]}'")
    except Exception:
        pass
    finally:
        import shutil
        try:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception:
            pass


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
        "version": "2026-04-26-v39",
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

    return redirect(url_for("catchup"))


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
    force_refresh = request.args.get("refresh") == "1"

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
        sessions = list_shared_sessions(force_refresh=force_refresh)
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
        <div class="card" data-sid="{sid}">
          <h3>{title}</h3>
          <div class="meta">{date} &middot; {dur} {badge}</div>
          <div class="card-action">{action}</div>
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
    no_volume_warn = "" if NOTES_PERSISTENT else """
    <div class="alert" style="border-color:#f59e0b;color:#fcd34d;font-size:0.82rem">
      <strong>&#9888; No persistent storage.</strong>
      Notes &amp; progress are saved in /tmp and will be lost if Railway restarts.<br>
      To fix: Railway dashboard &rarr; your service &rarr; <strong>New Volume</strong>
      &rarr; mount path <code>/data</code>. One-time setup, takes 30 seconds.
    </div>"""
    body = f"""
    <a href="/dashboard" style="color:#6366f1;font-size:0.875rem">← Dashboard</a>
    <h1 style="margin-top:8px">Lecture List</h1>
    <p class="sub">{len(sessions)} lectures since March 2026 &nbsp;
      <a href="/lectures?refresh=1" style="font-size:0.8rem;color:#6366f1">Check for new lectures</a>
    </p>
    {no_volume_warn}
    {err_html}
    {gen_all_btn}
    {''.join(cards)}
    <script>
    (function(){{
      document.querySelectorAll('[data-sid]').forEach(function(card){{
        var sid=card.getAttribute('data-sid');
        var stored=localStorage.getItem('pn_'+sid);
        if(!stored)return;
        var action=card.querySelector('.card-action');
        if(!action)return;
        // Don't override if server already shows a download link
        if(action.querySelector('a[href*="/download/"]'))return;
        var meta=JSON.parse(localStorage.getItem('pn_meta_'+sid)||'{{}}');
        card.querySelector('.meta').insertAdjacentHTML('beforeend',' <span class="badge badge-ready">Saved</span>');
        action.innerHTML='<button class="btn btn-success btn-sm btn-full" onclick="dlLocal(\''+sid+'\')">Download saved notes</button>';
      }});
      window.dlLocal=function(sid){{
        var notes=localStorage.getItem('pn_'+sid);
        if(!notes){{alert('Not found in browser storage.');return;}}
        var meta=JSON.parse(localStorage.getItem('pn_meta_'+sid)||'{{}}');
        var title=((meta.title)||'notes').replace(/[^\\w\\s-]/g,'').trim().replace(/\\s+/g,'_').substring(0,60)||'notes';
        var blob=new Blob([notes],{{type:'text/markdown'}});
        var a=document.createElement('a');
        a.href=URL.createObjectURL(blob);
        a.download=title+'.md';
        document.body.appendChild(a);a.click();document.body.removeChild(a);
      }};
    }})();
    </script>"""
    return PAGE.format(body=body)


# ---------------------------------------------------------------------------
# Background job status — stored in /tmp so both gunicorn workers can read it
# ---------------------------------------------------------------------------

def _job_path(job_id):
    return os.path.join(tempfile.gettempdir(), f"job_{job_id}.json")


def _set_job(job_id, **kwargs):
    with open(_job_path(job_id), "w") as f:
        json.dump(kwargs, f)


def _get_job(job_id):
    try:
        with open(_job_path(job_id)) as f:
            return json.load(f)
    except Exception:
        return {}


def _run_single(session_id):
    """Process one lecture in a background thread."""
    try:
        _set_job(session_id, status="working", msg="Fetching lecture details…")
        detail = get_session_detail(session_id)
        title = detail.get("Name", "Untitled")
        date = _fmt_date(detail.get("StartTime", ""))
        download_url = detail.get("DownloadUrl") or detail.get("Urls", {}).get("DownloadUrl")
        caption_url = detail.get("CaptionDownloadUrl") or detail.get("Urls", {}).get("CaptionDownloadUrl")

        _set_job(session_id, status="working", msg="Loading viewer &amp; extracting transcript…")
        transcript, method = download_and_transcribe(
            session_id, download_url, caption_url,
            status_cb=lambda m: _set_job(session_id, status="working", msg=m))

        if not transcript:
            detail_src = detail.get("_detail_source", "none")
            url_type = detail.get("_url_type", "none")
            _set_job(session_id, status="error",
                     msg=f"No transcript: {method} | detail_src={detail_src} url_type={url_type} "
                         f"dl={bool(download_url)} cap={bool(caption_url)}")
            return

        _set_job(session_id, status="working", msg="Generating notes with AI…")
        notes_md = generate_notes(title, date, transcript,
                                  status_cb=lambda m: _set_job(session_id, status="working", msg=m))
        with open(notes_path(session_id), "w") as f:
            f.write(notes_md)

        source_label = "captions" if "caption" in method else "Whisper"
        _set_job(session_id, status="done", msg=f"Notes ready ({source_label})", title=title, date=date)

        # Classify course in background — lightweight, just one small LLaMA call
        def _post_process():
            try:
                gc = Groq(api_key=os.environ["GROQ_API_KEY"])
                classify_lecture(session_id, title, date, notes_md, gc)
            except Exception:
                pass
        _threading.Thread(target=_post_process, daemon=True).start()

    except Exception as e:
        err_str = str(e)
        job_kwargs = {"status": "error", "msg": err_str[:300]}
        if err_str.startswith("RATE_LIMIT:"):
            m = re.search(r"(\d+)m(\d+)s", err_str)
            if m:
                wait_secs = int(m.group(1)) * 60 + int(m.group(2))
            else:
                m2 = re.search(r"(\d+)s\b", err_str)
                wait_secs = int(m2.group(1)) if m2 else 900
            job_kwargs["error_time"] = time.time()
            job_kwargs["wait_seconds"] = wait_secs
        _set_job(session_id, **job_kwargs)


def _run_all(ids):
    """Process all lectures sequentially, auto-retrying on rate limits."""
    with _run_all_lock:
        _run_all_locked(ids)


def _run_all_locked(ids):
    total = len(ids)
    completed, failed = [], []

    def _status(msg):
        _set_job("all", status="working", msg=msg,
                 completed=completed, failed=failed)

    def _wait_for_rate_limit(err_str, attempt, title):
        m = re.search(r"(\d+)m(\d+)s", err_str)
        if m:
            groq_wait = int(m.group(1)) * 60 + int(m.group(2))
        else:
            m2 = re.search(r"(\d+)s\b", err_str)
            groq_wait = int(m2.group(1)) if m2 else 300
        # Always trust Groq's stated retry-after time — rolling window is accurate.
        # Add a small buffer that grows with attempts; never jump to a full hour.
        buffer = min(30 + attempt * 20, 180)
        wait_secs = groq_wait + buffer
        deadline = time.time() + wait_secs
        while time.time() < deadline:
            rem = max(0, int(deadline - time.time()))
            mins, secs = divmod(rem, 60)
            _status(f"Rate limit — resuming '{title[:35]}' in {mins}m{secs:02d}s "
                    f"(attempt {attempt + 2}/15, no re-download needed)…")
            time.sleep(5)

    def _process_one(sid, i, total_n):
        """Try one lecture with up to 14 retries. Returns (success, title, error)."""
        try:
            detail = get_session_detail(sid)
        except Exception as e:
            return False, f"Lecture {i}", f"details failed: {str(e)[:60]}"
        title = detail.get("Name", "Untitled")
        date = _fmt_date(detail.get("StartTime", ""))
        dl = detail.get("DownloadUrl") or detail.get("Urls", {}).get("DownloadUrl")
        cap = detail.get("CaptionDownloadUrl") or detail.get("Urls", {}).get("CaptionDownloadUrl")
        for attempt in range(15):
            try:
                _status(f"Lecture {i}/{total_n}: {title[:45]}…")
                transcript, method = download_and_transcribe(sid, dl, cap, status_cb=_status)
                if not transcript:
                    return False, title, "no audio source"
                notes_md = generate_notes(title, date, transcript, status_cb=_status)
                with open(notes_path(sid), "w") as f:
                    f.write(notes_md)
                # Classify only — video generation removed from main pipeline
                try:
                    gc = Groq(api_key=os.environ["GROQ_API_KEY"])
                    classify_lecture(sid, title, date, notes_md, gc)
                except Exception:
                    pass
                return True, title, method
            except Exception as e:
                err_str = str(e)
                if err_str.startswith("RATE_LIMIT:") and attempt < 14:
                    _wait_for_rate_limit(err_str, attempt, title)
                else:
                    return False, title, str(e)[:120]
        return False, title, "max retries exceeded"

    # Main pass
    for i, sid in enumerate(ids, 1):
        _status(f"Lecture {i}/{total} — starting…")
        ok, title, info = _process_one(sid, i, total)
        if ok:
            completed.append(f"{title} ({info})")
        else:
            failed.append(f"{title} — {info}")

    # Retry pass — attempt every failed lecture once more after the main run
    if failed:
        retry_ids = [sid for sid in ids
                     if not os.path.exists(notes_path(sid))]
        if retry_ids:
            _status(f"Main pass done. Retrying {len(retry_ids)} failed lecture(s)…")
            time.sleep(300)  # wait 5 min before retry pass
            still_failed = []
            for i, sid in enumerate(retry_ids, 1):
                ok, title, info = _process_one(sid, i, len(retry_ids))
                if ok:
                    completed.append(f"{title} ({info}) [retry]")
                    failed = [f for f in failed if not f.startswith(title)]
                else:
                    still_failed.append(f"{title} — {info}")
            failed = [f for f in failed
                      if any(f.startswith(t) for t in [x.split(" —")[0] for x in still_failed])]
            failed = still_failed

    _set_job("all", status="done",
             msg=f"All done — {len(completed)} completed, {len(failed)} failed",
             completed=completed, failed=failed)
    # Clear the pending file so auto-resume doesn't restart a finished job
    try:
        os.unlink(_PENDING_ALL_PATH)
    except Exception:
        pass


@app.route("/process/<session_id>", methods=["POST"])
def process(session_id):
    _set_job(session_id, status="working", msg="Starting…")
    _threading.Thread(target=_run_single, args=(session_id,), daemon=True).start()
    return redirect(url_for("job_status", session_id=session_id))


@app.route("/status/<session_id>")
def job_status(session_id):
    job = _get_job(session_id)
    status = job.get("status", "")
    msg = job.get("msg", "")

    # Empty job = file doesn't exist, likely a container restart
    if not status:
        # Check if auto-resume is already running or pending
        pending_exists = os.path.exists(_PENDING_ALL_PATH)
        if pending_exists:
            body = """
            <h1>Resuming…</h1>
            <p class="sub">Server restarted — auto-resuming in a few seconds.</p>
            <div class="card"><div><span class="spinner"></span> Picking up where it stopped…</div></div>
            <meta http-equiv="refresh" content="6">"""
        else:
            body = f"""
            <h1>Job Not Found</h1>
            <div class="alert alert-err">
              The job was interrupted by a server restart.<br><br>
              Go back and tap <strong>Generate Notes</strong> again to restart.
            </div>
            <a href="/lectures" class="btn btn-primary btn-full">Back to lectures</a>"""
        return PAGE.format(body=body)

    if status == "done":
        title = job.get("title", "Lecture")
        date = job.get("date", "")
        title_js = json.dumps(title)
        date_js = json.dumps(date)
        body = f"""
        <h1>Notes Ready!</h1>
        <p class="sub">Generated successfully — saved to your browser</p>
        <div class="card">
          <h3>{title}</h3>
          <div class="meta">{date}</div>
          <a href="/download/{session_id}" class="btn btn-success btn-full" style="margin-top:12px">
            Download .md file
          </a>
        </div>
        <br>
        <a href="/lectures" class="btn btn-primary btn-full">Back to all lectures</a>
        <script>
        fetch('/download/{session_id}?inline=1')
          .then(function(r){{return r.ok?r.text():null;}})
          .then(function(txt){{
            if(!txt)return;
            try{{
              localStorage.setItem('pn_{session_id}',txt);
              localStorage.setItem('pn_meta_{session_id}',JSON.stringify({{title:{title_js},date:{date_js}}}));
            }}catch(e){{}}
          }});
        </script>"""
    elif status == "error":
        import html as _h
        msg_esc = _h.escape(msg)
        is_rate_limit = msg.startswith("RATE_LIMIT:")
        if is_rate_limit:
            error_time = job.get("error_time", time.time())
            wait_secs = job.get("wait_seconds", 900)
            remaining = max(0, int(wait_secs - (time.time() - error_time)))
            ready_now = "true" if remaining == 0 else "false"
            countdown_text = "Ready — tap Retry below" if remaining == 0 else f"Retry available in {remaining // 60}m {remaining % 60:02d}s"
            retry_btn = f"""
            <div style="margin-top:12px">
              <div id="cd" style="text-align:center;font-size:0.95rem;margin-bottom:10px;
                   color:{'#6ee7b7' if remaining == 0 else '#94a3b8'}">{countdown_text}</div>
              <form method="post" action="/process/{session_id}" id="rf">
                <button class="btn btn-primary btn-full" id="rb" type="submit"
                        {'style=""' if remaining == 0 else 'disabled style="opacity:0.45;cursor:not-allowed"'}>
                  Retry (resumes where it stopped)
                </button>
              </form>
            </div>
            <script>
            (function(){{
              var deadline=Date.now()+{remaining}*1000, ready={ready_now};
              if(ready)return;
              var btn=document.getElementById('rb');
              var cd=document.getElementById('cd');
              var iv=setInterval(function(){{
                var r=Math.max(0,Math.round((deadline-Date.now())/1000));
                if(r<=0){{
                  clearInterval(iv);
                  cd.textContent='Ready — tap Retry below';
                  cd.style.color='#6ee7b7';
                  btn.disabled=false; btn.style.opacity='1'; btn.style.cursor='pointer';
                }}else{{
                  var m=Math.floor(r/60), s=r%60;
                  cd.textContent='Retry available in '+m+'m '+(s<10?'0':'')+s+'s';
                }}
              }},500);
            }})();
            </script>"""
        else:
            retry_btn = ""
        body = f"""
        <h1>{"Rate Limit" if is_rate_limit else "Error"}</h1>
        <div class="alert alert-err">{msg_esc}</div>
        {retry_btn}
        <br><a href="/lectures" class="btn btn-sm" style="color:#64748b">Back to lectures</a>"""
    else:
        body = f"""
        <h1>Generating Notes…</h1>
        <p class="sub">Keep this page open — it updates automatically.</p>
        <div class="card">
          <div><span class="spinner"></span> {msg}</div>
          <div class="meta" style="margin-top:8px">This takes 1–5 min depending on lecture length.</div>
        </div>
        <meta http-equiv="refresh" content="5">"""

    return PAGE.format(body=body)


@app.route("/process-all", methods=["POST"])
def process_all():
    ids = json.loads(request.form.get("ids", "[]"))
    # Persist IDs so the job auto-resumes if the container restarts
    try:
        with open(_PENDING_ALL_PATH, "w") as f:
            json.dump(ids, f)
    except Exception:
        pass
    _set_job("all", status="working", msg="Starting…", completed=[], failed=[])
    _threading.Thread(target=_run_all, args=(ids,), daemon=True).start()
    return redirect(url_for("job_all_status"))


@app.route("/status-all")
def job_all_status():
    job = _get_job("all")
    status = job.get("status", "working")
    completed = job.get("completed", [])
    failed = job.get("failed", [])
    msg = job.get("msg", "Working…")

    done_list = "".join(f"<li>{t}</li>" for t in completed)
    fail_list = "".join(f"<li style='word-break:break-all;font-size:0.75rem'>{t}</li>" for t in failed)
    fail_html = f'<div class="alert alert-err"><strong>Failed ({len(failed)}):</strong><ul style="margin-top:6px;padding-left:16px">{fail_list}</ul></div>' if failed else ""

    if status == "done":
        body = f"""
        <h1>All Done!</h1>
        <p class="sub">{len(completed)} lectures completed, {len(failed)} failed</p>
        {fail_html}
        <div class="card">
          <strong style="color:#6ee7b7">Completed:</strong>
          <ul style="margin-top:8px;padding-left:16px;font-size:0.875rem">{done_list or '<li style=color:#64748b>none</li>'}</ul>
        </div>
        <br>
        <a href="/lectures" class="btn btn-success btn-full">Back to lectures to download</a>"""
    else:
        body = f"""
        <h1>Generating All Notes…</h1>
        <p class="sub">Keep this page open — it updates every 8 seconds.</p>
        <div class="card">
          <div><span class="spinner"></span> {msg}</div>
          <div class="meta" style="margin-top:8px">
            Completed: {len(completed)} &middot; Failed: {len(failed)}
          </div>
        </div>
        {fail_html}
        {chr(10).join('<div class="card" style="border-color:#065f46"><div class="meta">' + t + '</div></div>' for t in completed[-3:])}
        <meta http-equiv="refresh" content="8">"""

    return PAGE.format(body=body)




@app.route("/download/<session_id>")
def download(session_id):
    path = notes_path(session_id)
    if not os.path.exists(path):
        if request.args.get("inline"):
            return "not found", 404
        return redirect(url_for("lectures", error="Notes file not found. Please generate notes first."))
    job = _get_job(session_id)
    title = job.get("title") or _sessions_cache.get(session_id, {}).get("Name", "")
    if title:
        safe = re.sub(r"[^\w\s-]", "", title).strip().replace(" ", "_")[:60]
        fname = f"{safe}.md"
    else:
        fname = f"notes_{session_id[:8]}.md"
    # ?inline=1 returns raw text so JS can save it to localStorage
    if request.args.get("inline"):
        with open(path, encoding="utf-8") as f:
            return f.read(), 200, {"Content-Type": "text/plain; charset=utf-8"}
    return send_file(
        path,
        as_attachment=True,
        download_name=fname,
        mimetype="text/markdown",
    )


# ---------------------------------------------------------------------------
# Dashboard routes
# ---------------------------------------------------------------------------

@app.route("/catchup")
def catchup():
    """The main study page: all courses, all lectures, clear status, missing ones prominent."""
    import html as _h
    metas = get_all_lecture_meta()
    done_ids = {m["session_id"] for m in metas}

    # Also pull the full Panopto session list to show unprocessed lectures
    all_sessions = []
    try:
        if session_is_ready():
            all_sessions = list_shared_sessions(force_refresh=False)
    except Exception:
        pass

    # Build a map of session_id → session info for unprocessed ones
    missing = [s for s in all_sessions if s.get("Id") and s["Id"] not in done_ids]

    # Group done lectures by course
    courses = {}
    for m in metas:
        c = m.get("course") or "Uncategorised"
        courses.setdefault(c, []).append(m)

    # Sort each course's lectures by date ascending (chronological)
    for c in courses:
        courses[c].sort(key=lambda m: m.get("date", ""))

    job = _get_job("all")
    processing_html = ""
    if job.get("status") == "working":
        processing_html = f"""
        <div style="background:#1e3a5f;border:1px solid #3b82f6;border-radius:10px;
                    padding:12px 16px;margin-bottom:16px;font-size:0.875rem">
          <span class="spinner"></span> {job.get("msg","Working…")}
          <a href="/status-all" style="color:#93c5fd;float:right">details →</a>
        </div>"""

    deepgram_tip = "" if os.environ.get("DEEPGRAM_API_KEY") else """
    <div class="alert" style="border-color:#f59e0b;color:#fcd34d;font-size:0.82rem;margin-bottom:16px">
      <strong>⚡ Speed up transcription:</strong> Sign up free at deepgram.com →
      get API key → add <code>DEEPGRAM_API_KEY</code> in Railway Variables.
      Transcribes 3-hour lectures in minutes instead of hours.
    </div>"""

    sections = []

    # Missing / unprocessed lectures first — most urgent
    if missing:
        ids_json = json.dumps([s["Id"] for s in missing])
        rows = ""
        for s in missing:
            t = _h.escape(s.get("Name", "Untitled"))
            d = _fmt_date(s.get("StartTime", ""))
            dur = _fmt_duration(s.get("Duration"))
            rows += (f'<div style="padding:10px 0;border-bottom:1px solid #1e293b;'
                     f'display:flex;align-items:center;gap:10px">'
                     f'<span style="color:#ef4444;font-size:1.1rem">✗</span>'
                     f'<div style="flex:1"><div style="font-size:0.9rem;color:#f1f5f9">{t}</div>'
                     f'<div style="font-size:0.75rem;color:#64748b">{d} · {dur}</div></div>'
                     f'</div>')
        sections.append(f"""
        <div class="card" style="border-color:#ef4444;margin-bottom:20px">
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
            <h3 style="color:#fca5a5">⚠ Missing Notes ({len(missing)} lectures)</h3>
            <form method="post" action="/process-all" style="margin:0">
              <input type="hidden" name="ids" value='{ids_json}'>
              <button class="btn btn-primary btn-sm" type="submit">Generate All</button>
            </form>
          </div>
          {rows}
        </div>""")

    # Done lectures by course
    for course_name in sorted(courses.keys()):
        lectures = courses[course_name]
        slug = _course_slug(course_name)
        lecturer = lectures[0].get("lecturer", "") if lectures else ""
        rows = ""
        for m in lectures:
            sid = m["session_id"]
            t = _h.escape(m.get("title", "Untitled"))
            d = m.get("date", "")
            has_video = os.path.exists(os.path.join(NOTES_DIR, f"video_{sid}.mp4"))
            media = "🎬" if has_video else ""
            rows += (f'<div style="padding:10px 0;border-bottom:1px solid #1e293b;'
                     f'display:flex;align-items:center;gap:10px">'
                     f'<span style="color:#10b981;font-size:1.1rem">✓</span>'
                     f'<div style="flex:1">'
                     f'<a href="/lecture/{sid}/notes-view" '
                     f'style="color:#f1f5f9;font-size:0.9rem;text-decoration:none">{t}</a>'
                     f'<div style="font-size:0.75rem;color:#64748b">{d} {media}</div></div>'
                     f'<a href="/lecture/{sid}/notes-view" class="btn btn-primary btn-sm">Notes</a>'
                     f'</div>')
        sections.append(f"""
        <div class="card" style="margin-bottom:16px">
          <h3 style="margin-bottom:2px">{_h.escape(course_name)}</h3>
          <div class="meta" style="margin-bottom:10px">{_h.escape(lecturer)} · {len(lectures)} lectures</div>
          {rows}
        </div>""")

    total_done = len(metas)
    total_all = total_done + len(missing)
    body = f"""
    <h1>Catch Up</h1>
    <p class="sub">{total_done}/{total_all} lectures have notes
      &nbsp;·&nbsp; <a href="/catchup" style="color:#6366f1;font-size:0.8rem">Refresh</a>
    </p>
    <a href="/chat" class="btn btn-primary btn-full" style="margin-bottom:16px">
      💬 Ask the study chatbot
    </a>
    {deepgram_tip}
    {processing_html}
    {''.join(sections) if sections else
     '<div class="card"><p style="color:#64748b">No lectures found. '
     '<a href="/lectures" style="color:#6366f1">Connect to Panopto →</a></p></div>'}
    """
    return PAGE.format(body=body)


@app.route("/dashboard")
def dashboard():
    metas = get_all_lecture_meta()

    # Group by course
    courses = {}
    for m in metas:
        c = m.get("course", "Uncategorised")
        if c not in courses:
            courses[c] = {"lecturer": m.get("lecturer", "Unknown"),
                          "lectures": [], "tasks": []}
        courses[c]["lectures"].append(m)
        courses[c]["tasks"].extend(m.get("tasks", []))

    job = _get_job("all")
    processing_html = ""
    if job.get("status") == "working":
        processing_html = f"""
        <div class="card" style="border-color:#6366f1;margin-bottom:16px">
          <div><span class="spinner"></span> {job.get("msg", "Working…")}</div>
          <a href="/status-all" style="font-size:0.8rem;color:#6366f1;display:block;margin-top:8px">View progress →</a>
        </div>"""

    if not courses:
        body = f"""
        <h1>My Courses</h1>
        {processing_html}
        <div class="card">
          <p style="color:#64748b">No lectures processed yet.</p>
          <a href="/lectures" class="btn btn-primary btn-full" style="margin-top:12px">Go to lecture list →</a>
        </div>"""
        return PAGE.format(body=body)

    course_cards = []
    for course_name, info in sorted(courses.items()):
        slug = _course_slug(course_name)
        lec_count = len(info["lectures"])
        task_count = len(info["tasks"])
        latest = info["lectures"][0].get("date", "") if info["lectures"] else ""
        task_badge = (f'<span class="badge" style="background:#4c1d95;color:#ddd8fe;margin-right:4px">'
                      f'{task_count} task{"s" if task_count != 1 else ""}</span>') if task_count else ""
        video_count = sum(1 for lm in info["lectures"] if lm.get("has_video"))
        audio_count = sum(1 for lm in info["lectures"] if lm.get("has_explainer") and not lm.get("has_video"))
        media_badge = ""
        if video_count:
            media_badge += (f'<span class="badge" style="background:#0f4c75;color:#93c5fd;margin-right:4px">'
                            f'🎬 {video_count} video{"s" if video_count != 1 else ""}</span>')
        if audio_count:
            media_badge += (f'<span class="badge" style="background:#0f4c75;color:#93c5fd">'
                            f'🎧 {audio_count}</span>')
        course_cards.append(f"""
        <div class="card" onclick="location.href='/course/{slug}'"
             style="cursor:pointer;border-left:3px solid #6366f1">
          <h3>{course_name}</h3>
          <div class="meta">{info["lecturer"]} &middot; {lec_count} lectures &middot; {latest}</div>
          <div style="margin-top:8px">{task_badge}{media_badge}</div>
        </div>""")

    total_tasks = sum(len(v["tasks"]) for v in courses.values())
    missing_video = sum(1 for m in metas if not m.get("has_video"))
    video_btn = ""
    if missing_video and not _explainer_lock.locked():
        video_btn = f"""
        <form method="post" action="/generate-explainers" style="margin-top:8px">
          <button class="btn btn-sm btn-full" type="submit"
                  style="background:#0f4c75;color:#93c5fd">
            🎬 Generate {missing_video} missing video{"s" if missing_video != 1 else ""}
          </button>
        </form>"""
    elif _explainer_lock.locked():
        video_btn = """
        <div class="card" style="margin-top:8px;border-color:#0f4c75">
          <span class="spinner"></span> Generating videos…
        </div>"""

    body = f"""
    <h1>My Courses</h1>
    <p class="sub">{len(metas)} lectures processed &nbsp;·&nbsp;
      auto-checks hourly &nbsp;
      <a href="/lectures?refresh=1" style="font-size:0.8rem;color:#6366f1">Check now</a>
    </p>
    {processing_html}
    {''.join(course_cards)}
    <a href="/tasks" class="btn btn-primary btn-full" style="margin-top:8px">
      📋 All Tasks{f' ({total_tasks})' if total_tasks else ''}
    </a>
    {video_btn}
    <a href="/lectures" class="btn btn-sm btn-full"
       style="background:#1e293b;color:#64748b;margin-top:8px">
      Lecture list &amp; Generate Notes
    </a>"""
    return PAGE.format(body=body)


_explainer_lock = _threading.Lock()

@app.route("/generate-explainers", methods=["POST"])
def generate_explainers():
    """Trigger audio explainer generation for all lectures that don't have one."""
    def _run():
        with _explainer_lock:
            try:
                gc = Groq(api_key=os.environ["GROQ_API_KEY"])
            except Exception:
                return
            for fname in sorted(os.listdir(NOTES_DIR)):
                if not (fname.startswith("notes_") and fname.endswith(".md")):
                    continue
                sid = fname[6:-3]
                if os.path.exists(os.path.join(NOTES_DIR, f"video_{sid}.mp4")):
                    continue
                try:
                    with open(os.path.join(NOTES_DIR, fname), encoding="utf-8") as f:
                        notes_md = f.read()
                    meta = _load_meta(sid)
                    t_m = re.search(r"^# (.+)$", notes_md, re.MULTILINE)
                    d_m = re.search(r"\*\*Date:\*\* (.+)$", notes_md, re.MULTILINE)
                    title = t_m.group(1) if t_m else meta.get("title", "Untitled")
                    date = d_m.group(1).strip() if d_m else meta.get("date", "")
                    generate_lecture_video(sid, title, date, notes_md, groq_client=gc)
                    time.sleep(5)
                except Exception:
                    pass
    if not _explainer_lock.locked():
        _threading.Thread(target=_run, daemon=True).start()
    return redirect(url_for("dashboard"))


@app.route("/course/<slug>")
def course_view(slug):
    metas = get_all_lecture_meta()
    course_lectures = [m for m in metas
                       if _course_slug(m.get("course", "Uncategorised")) == slug]
    if not course_lectures:
        return redirect(url_for("dashboard"))

    course_name = course_lectures[0].get("course", slug)
    lecturer = course_lectures[0].get("lecturer", "Unknown")

    all_tasks = [(t, m.get("title", ""), m.get("date", ""))
                 for m in course_lectures for t in m.get("tasks", [])]

    tasks_html = ""
    if all_tasks:
        items = "".join(
            f'<li style="margin-bottom:8px">'
            f'<div style="font-size:0.875rem">{t}</div>'
            f'<div style="font-size:0.75rem;color:#64748b;margin-top:2px">{lec[:40]} · {d}</div>'
            f'</li>'
            for t, lec, d in all_tasks)
        tasks_html = f"""
        <div class="card" style="border-color:#7c3aed;margin-bottom:16px">
          <h3 style="color:#c4b5fd;margin-bottom:10px">📋 Tasks ({len(all_tasks)})</h3>
          <ul style="padding-left:16px;list-style:disc">{items}</ul>
        </div>"""

    lecture_cards = []
    for m in course_lectures:
        sid = m["session_id"]
        title = m.get("title", "Untitled")
        date = m.get("date", "")
        has_video = os.path.exists(os.path.join(NOTES_DIR, f"video_{sid}.mp4"))
        has_audio = m.get("has_explainer", False)
        media_html = ""
        if has_video:
            media_html = (f'<video controls style="width:100%;border-radius:8px;margin:10px 0;'
                          f'background:#000;max-height:360px" src="/lecture/{sid}/video">'
                          f'Your browser does not support video.</video>')
        elif has_audio:
            media_html = (f'<audio controls style="width:100%;margin:10px 0" '
                          f'src="/lecture/{sid}/audio"></audio>')
        lecture_cards.append(f"""
        <div class="card">
          <h3>{title}</h3>
          <div class="meta">{date}</div>
          {media_html}
          <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:8px">
            <a href="/lecture/{sid}/notes-view" class="btn btn-primary btn-sm">📄 Notes</a>
            <a href="/download/{sid}" class="btn btn-sm"
               style="background:#1e293b;color:#94a3b8">⬇ .md</a>
          </div>
        </div>""")

    body = f"""
    <a href="/dashboard" style="color:#6366f1;font-size:0.875rem">← All Courses</a>
    <h1 style="margin-top:8px">{course_name}</h1>
    <p class="sub">{lecturer} &middot; {len(course_lectures)} lectures</p>
    {tasks_html}
    {''.join(lecture_cards)}"""
    return PAGE.format(body=body)


@app.route("/tasks")
def tasks_view():
    metas = get_all_lecture_meta()
    course_tasks = {}
    for m in metas:
        if not m.get("tasks"):
            continue
        c = m.get("course", "Uncategorised")
        course_tasks.setdefault(c, [])
        for t in m["tasks"]:
            course_tasks[c].append((t, m.get("title", ""), m.get("date", "")))

    if not course_tasks:
        body = """
        <a href="/dashboard" style="color:#6366f1;font-size:0.875rem">← Dashboard</a>
        <h1 style="margin-top:8px">All Tasks</h1>
        <div class="card"><p style="color:#64748b">No tasks found in any lecture yet.</p></div>"""
        return PAGE.format(body=body)

    sections = []
    for course, tasks in sorted(course_tasks.items()):
        items = "".join(
            f'<li style="margin-bottom:10px">'
            f'<div style="font-size:0.875rem">{t}</div>'
            f'<div style="font-size:0.75rem;color:#64748b;margin-top:2px">{lec[:40]} · {d}</div>'
            f'</li>'
            for t, lec, d in tasks)
        sections.append(f"""
        <div class="card">
          <h3 style="color:#c4b5fd;margin-bottom:10px">{course}</h3>
          <ul style="padding-left:16px;list-style:disc">{items}</ul>
        </div>""")

    total = sum(len(v) for v in course_tasks.values())
    body = f"""
    <a href="/dashboard" style="color:#6366f1;font-size:0.875rem">← Dashboard</a>
    <h1 style="margin-top:8px">All Tasks</h1>
    <p class="sub">{total} tasks across {len(course_tasks)} courses</p>
    {''.join(sections)}"""
    return PAGE.format(body=body)


@app.route("/lecture/<session_id>/video")
def lecture_video(session_id):
    path = os.path.join(NOTES_DIR, f"video_{session_id}.mp4")
    if not os.path.exists(path):
        return "Video not ready yet", 404
    return send_file(path, mimetype="video/mp4", conditional=True)


@app.route("/lecture/<session_id>/audio")
def lecture_audio(session_id):
    audio_path = os.path.join(NOTES_DIR, f"explainer_{session_id}.mp3")
    if not os.path.exists(audio_path):
        return "Audio explainer not ready yet", 404
    return send_file(audio_path, mimetype="audio/mpeg", conditional=True)


@app.route("/lecture/<session_id>/notes-view")
def lecture_notes_view(session_id):
    import html as _html
    path = notes_path(session_id)
    if not os.path.exists(path):
        return redirect(url_for("dashboard"))
    with open(path, encoding="utf-8") as f:
        raw = f.read()
    meta = _load_meta(session_id)
    title = meta.get("title", "Lecture Notes")
    course = meta.get("course", "")
    back = f"/course/{_course_slug(course)}" if course else "/dashboard"

    # Lightweight markdown → HTML (no external lib needed)
    html_content = _html.escape(raw)
    html_content = re.sub(r"^# (.+)$",
        r'<h2 style="font-size:1.3rem;color:#f1f5f9;margin:20px 0 6px">\1</h2>',
        html_content, flags=re.MULTILINE)
    html_content = re.sub(r"^## (.+)$",
        r'<h3 style="font-size:1.05rem;color:#c4b5fd;margin:16px 0 4px">\1</h3>',
        html_content, flags=re.MULTILINE)
    html_content = re.sub(r"^### (.+)$",
        r'<h4 style="font-size:0.95rem;color:#93c5fd;margin:12px 0 3px">\1</h4>',
        html_content, flags=re.MULTILINE)
    html_content = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", html_content)
    html_content = re.sub(r"^- (.+)$",
        r'<li style="margin:3px 0 3px 16px">\1</li>', html_content, flags=re.MULTILINE)
    html_content = html_content.replace("\n\n", '<br style="margin:6px 0">')

    has_explainer = os.path.exists(os.path.join(NOTES_DIR, f"explainer_{session_id}.mp3"))
    audio_html = ""
    if has_explainer:
        audio_html = (f'<audio controls style="width:100%;margin-bottom:16px;border-radius:6px" '
                      f'src="/lecture/{session_id}/audio">Your browser does not support audio.</audio>')

    body = f"""
    <a href="{back}" style="color:#6366f1;font-size:0.875rem">← {course or 'Dashboard'}</a>
    <h1 style="margin-top:8px;font-size:1.15rem">{_html.escape(title)}</h1>
    <a href="/download/{session_id}" class="btn btn-sm"
       style="background:#1e293b;color:#94a3b8;display:inline-block;margin-bottom:14px">
      ⬇ Download .md
    </a>
    {audio_html}
    <div style="font-size:0.875rem;line-height:1.75;color:#cbd5e1">
      {html_content}
    </div>"""
    return PAGE.format(body=body)


CHAT_SYSTEM_PROMPT = """You are a helpful university study assistant. You have access to a student's lecture notes.
Answer their question using ONLY the provided notes. Be specific and direct.
- If they ask about homework/tasks/deadlines: list every one mentioned clearly.
- If they ask about a concept: explain it from the notes with any examples given.
- If they ask what they missed: summarise the key points.
- If the answer is not in the notes provided: say "I don't have notes for that yet."
Never make up content not in the notes."""

_ORDINALS = {
    "first": 1, "1st": 1, "second": 2, "2nd": 2, "third": 3, "3rd": 3,
    "fourth": 4, "4th": 4, "fifth": 5, "5th": 5, "sixth": 6, "6th": 6,
    "seventh": 7, "7th": 7, "eighth": 8, "8th": 8, "ninth": 9, "9th": 9,
    "tenth": 10, "10th": 10,
}


def _build_chat_context(question):
    """Return (context_text, matched_course, matched_lecture_title) for the question."""
    metas = get_all_lecture_meta()
    if not metas:
        return "No lecture notes available yet.", None, None

    q = question.lower()

    # Detect ordinal ("second lecture", "lecture 3")
    target_index = None
    for word, idx in _ORDINALS.items():
        if word in q:
            target_index = idx
            break
    m_num = re.search(r"lecture\s+(\d+)", q)
    if m_num:
        target_index = int(m_num.group(1))

    # Score each lecture's relevance to the question
    def _score(m):
        score = 0
        course = m.get("course", "").lower()
        title = m.get("title", "").lower()
        # Course name words in question
        for word in course.split():
            if len(word) > 3 and word in q:
                score += 3
        # Title words in question
        for word in title.split():
            if len(word) > 4 and word in q:
                score += 1
        return score

    scored = sorted(metas, key=_score, reverse=True)
    best_score = _score(scored[0]) if scored else 0

    # If a specific course matched, narrow to that course then apply ordinal
    if best_score >= 3:
        best_course = scored[0].get("course", "")
        course_lectures = sorted(
            [m for m in metas if m.get("course") == best_course],
            key=lambda m: m.get("date", "")
        )
        if target_index and 1 <= target_index <= len(course_lectures):
            selected = [course_lectures[target_index - 1]]
        else:
            selected = course_lectures  # all lectures in that course
        matched_course = best_course
    else:
        # General question — use top-scored lectures
        selected = scored[:5] if not target_index else scored[:3]
        matched_course = None

    # Build context from selected lectures
    parts = []
    for m in selected[:6]:  # cap at 6 notes to stay within token limits
        sid = m["session_id"]
        np = notes_path(sid)
        if os.path.exists(np):
            with open(np, encoding="utf-8") as f:
                content = f.read()[:4000]
            parts.append(
                f"=== {m.get('course','')} — {m.get('title','')} ({m.get('date','')}) ===\n{content}"
            )

    matched_title = selected[0].get("title") if selected else None
    return "\n\n---\n\n".join(parts) if parts else "No notes found.", matched_course, matched_title


@app.route("/chat")
def chat_page():
    import html as _h
    metas = get_all_lecture_meta()
    courses = sorted({m.get("course", "Uncategorised") for m in metas})
    course_links = " &nbsp;·&nbsp; ".join(
        f'<a href="#" onclick="ask(\'Summarise {_h.escape(c)}\')" '
        f'style="color:#6366f1;font-size:0.8rem">{_h.escape(c)}</a>'
        for c in courses
    )
    body = f"""
    <h1>Study Chat</h1>
    <p class="sub">Ask anything about your lectures, homework, or course content.</p>
    <div style="font-size:0.8rem;color:#64748b;margin-bottom:12px">
      Courses: {course_links or 'none yet'}
    </div>
    <div id="msgs" style="min-height:200px;margin-bottom:12px">
      <div class="card" style="border-color:#334155;color:#64748b;font-size:0.875rem">
        Try: &ldquo;What homework do I have for research seminar?&rdquo;<br>
        Or: &ldquo;Summarise the second lecture of business law&rdquo;<br>
        Or: &ldquo;What are the key topics I need to know for the exam?&rdquo;
      </div>
    </div>
    <div style="display:flex;gap:8px;align-items:flex-end">
      <textarea id="q" rows="2" placeholder="Ask anything…"
        style="flex:1;padding:10px 12px;border-radius:8px;background:#1e293b;
               border:1px solid #334155;color:#e2e8f0;font-size:1rem;
               resize:none;font-family:inherit"
        onkeydown="if(event.key==='Enter'&&!event.shiftKey){{event.preventDefault();send();}}">
      </textarea>
      <button onclick="send()" class="btn btn-primary" id="sb" style="height:48px;padding:0 20px">
        Send
      </button>
    </div>
    <a href="/catchup" style="display:block;margin-top:16px;color:#6366f1;font-size:0.8rem">
      ← Back to courses
    </a>
    <script>
    function ask(q){{document.getElementById('q').value=q;send();}}
    function send(){{
      var q=document.getElementById('q').value.trim();
      if(!q)return;
      var msgs=document.getElementById('msgs');
      msgs.innerHTML+=
        '<div class="card" style="border-color:#6366f1;margin-bottom:10px">'
        +'<div style="font-size:0.75rem;color:#6366f1;margin-bottom:4px">You</div>'
        +'<div style="font-size:0.9rem">'+q.replace(/</g,'&lt;')+'</div></div>';
      document.getElementById('q').value='';
      document.getElementById('sb').disabled=true;
      var wait=document.createElement('div');
      wait.className='card';
      wait.id='wait';
      wait.style.marginBottom='10px';
      wait.innerHTML='<span class="spinner"></span> Thinking…';
      msgs.appendChild(wait);
      msgs.scrollTop=msgs.scrollHeight;
      fetch('/chat/ask',{{method:'POST',headers:{{'Content-Type':'application/json'}},
        body:JSON.stringify({{message:q}})}})
        .then(function(r){{return r.json();}})
        .then(function(d){{
          var w=document.getElementById('wait');
          if(w)w.remove();
          var txt=(d.response||d.error||'Sorry, something went wrong.')
            .replace(/\\n/g,'<br>').replace(/\\*\\*(.+?)\\*\\*/g,'<strong>$1</strong>');
          msgs.innerHTML+=
            '<div class="card" style="border-color:#10b981;margin-bottom:10px">'
            +'<div style="font-size:0.75rem;color:#10b981;margin-bottom:4px">Assistant</div>'
            +'<div style="font-size:0.9rem;line-height:1.6">'+txt+'</div></div>';
          msgs.scrollTop=msgs.scrollHeight;
          document.getElementById('sb').disabled=false;
        }})
        .catch(function(){{
          var w=document.getElementById('wait');if(w)w.remove();
          document.getElementById('sb').disabled=false;
        }});
    }}
    </script>"""
    return PAGE.format(body=body)


@app.route("/chat/ask", methods=["POST"])
def chat_ask():
    from flask import jsonify
    data = request.get_json(silent=True) or {}
    question = (data.get("message") or "").strip()
    if not question:
        return jsonify({"error": "Empty question"}), 400
    try:
        context, matched_course, matched_title = _build_chat_context(question)
        gc = Groq(api_key=os.environ["GROQ_API_KEY"])
        answer = gc.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": CHAT_SYSTEM_PROMPT},
                {"role": "user", "content": f"Lecture notes:\n{context}\n\nQuestion: {question}"},
            ],
            temperature=0.3,
            max_tokens=900,
        ).choices[0].message.content
        return jsonify({"response": answer})
    except Exception as e:
        return jsonify({"error": str(e)[:200]}), 500


def _startup_warmup():
    """Pre-warm SSO and auto-resume any Generate All job interrupted by a restart."""
    time.sleep(8)
    if os.environ.get("MOODLE_USERNAME") or os.environ.get("PANOPTO_COOKIE") or _runtime_cookie:
        try:
            get_session()
        except Exception:
            pass

    # Auto-resume Generate All if the container restarted mid-job
    try:
        with open(_PENDING_ALL_PATH) as f:
            all_ids = json.load(f)
        # Only include lectures that don't have notes yet
        remaining = [sid for sid in all_ids if not os.path.exists(notes_path(sid))]
        if remaining and not _run_all_lock.locked():
            _set_job("all", status="working",
                     msg=f"Auto-resuming after restart — {len(remaining)} lectures remaining…",
                     completed=[], failed=[])
            _threading.Thread(target=_run_all, args=(remaining,), daemon=True).start()
    except Exception:
        pass  # No pending job, nothing to resume

    # Backfill: classify any already-processed lectures that lack course metadata
    def _backfill():
        time.sleep(60)
        # Don't compete with an active Generate All job
        if _run_all_lock.locked():
            time.sleep(600)
        try:
            gc = Groq(api_key=os.environ["GROQ_API_KEY"])
        except Exception:
            return
        try:
            for fname in sorted(os.listdir(NOTES_DIR)):
                if not (fname.startswith("notes_") and fname.endswith(".md")):
                    continue
                sid = fname[6:-3]
                meta = _load_meta(sid)
                needs_classify = not meta.get("course")
                needs_video = not os.path.exists(
                    os.path.join(NOTES_DIR, f"video_{sid}.mp4"))
                if not needs_classify and not needs_video:
                    continue
                try:
                    with open(os.path.join(NOTES_DIR, fname), encoding="utf-8") as f:
                        notes_md = f.read()
                    t_match = re.search(r"^# (.+)$", notes_md, re.MULTILINE)
                    d_match = re.search(r"\*\*Date:\*\* (.+)$", notes_md, re.MULTILINE)
                    title = t_match.group(1) if t_match else meta.get("title", "Untitled")
                    date = d_match.group(1).strip() if d_match else meta.get("date", "")
                    if needs_classify:
                        classify_lecture(sid, title, date, notes_md, gc)
                        time.sleep(3)
                    if needs_video:
                        generate_lecture_video(sid, title, date, notes_md, groq_client=gc)
                        time.sleep(5)
                except Exception:
                    pass
        except Exception:
            pass
    _threading.Thread(target=_backfill, daemon=True).start()

    # Auto-poll: check Panopto every hour for new lectures, process automatically
    def _auto_poll():
        time.sleep(3600)  # first check 1h after startup
        while True:
            try:
                if session_is_ready() and not _run_all_lock.locked():
                    sessions = list_shared_sessions(force_refresh=True)
                    new_ids = [s["Id"] for s in sessions
                               if not os.path.exists(notes_path(s["Id"]))]
                    if new_ids:
                        try:
                            with open(_PENDING_ALL_PATH, "w") as f:
                                json.dump(new_ids, f)
                        except Exception:
                            pass
                        _set_job("all", status="working",
                                 msg=f"Auto-processing {len(new_ids)} new lecture(s)…",
                                 completed=[], failed=[])
                        _threading.Thread(target=_run_all, args=(new_ids,), daemon=True).start()
            except Exception:
                pass
            time.sleep(3600)
    _threading.Thread(target=_auto_poll, daemon=True).start()

_threading.Thread(target=_startup_warmup, daemon=True).start()


if __name__ == "__main__":
    app.run(debug=True, port=5000)
