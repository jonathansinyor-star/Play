"""Telegram authentication flow via web browser.

Adds two endpoints to the dashboard:
  GET  /auth        → shows a form to enter the verification code
  POST /auth/verify → submits the code and completes login

This allows interactive Telegram login without needing a terminal.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import Form
from fastapi.responses import HTMLResponse

log = logging.getLogger(__name__)

# Shared state between auth flow and main app
_pending_client: Any = None
_phone_code_future: asyncio.Future | None = None
_auth_status: str = "idle"  # idle | waiting_code | success | error
_auth_error: str = ""


def set_pending_client(client: Any, future: asyncio.Future) -> None:
    global _pending_client, _phone_code_future, _auth_status
    _pending_client = client
    _phone_code_future = future
    _auth_status = "waiting_code"


def mark_auth_success() -> None:
    global _auth_status
    _auth_status = "success"


def mark_auth_error(msg: str) -> None:
    global _auth_status, _auth_error
    _auth_status = "error"
    _auth_error = msg


def register_auth_routes(app) -> None:
    """Register auth routes onto the FastAPI app."""

    @app.get("/auth", response_class=HTMLResponse)
    async def auth_page():
        if _auth_status == "success":
            return HTMLResponse(_success_html())
        if _auth_status == "error":
            return HTMLResponse(_error_html(_auth_error))
        return HTMLResponse(_auth_form_html())

    @app.post("/auth/verify", response_class=HTMLResponse)
    async def auth_verify(code: str = Form(...)):
        global _phone_code_future
        if _phone_code_future is None or _phone_code_future.done():
            return HTMLResponse(_error_html("No pending login session. Redeploy the app to try again."))
        try:
            _phone_code_future.set_result(code.strip())
            # Give it a moment to process
            await asyncio.sleep(2)
            if _auth_status == "success":
                return HTMLResponse(_success_html())
            return HTMLResponse(_waiting_html())
        except Exception as exc:
            return HTMLResponse(_error_html(str(exc)))


def _auth_form_html() -> str:
    return """<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>Telegram Login – Beirut Monitor</title>
  <style>
    body { background: #0d1117; color: #c9d1d9; font-family: system-ui, sans-serif;
           display: flex; align-items: center; justify-content: center; height: 100vh; margin: 0; }
    .box { background: #161b22; border: 1px solid #30363d; border-radius: 12px;
           padding: 40px; max-width: 400px; width: 90%; text-align: center; }
    h2 { color: #fff; margin-bottom: 8px; }
    p { color: #8b949e; margin-bottom: 24px; line-height: 1.5; }
    input { width: 100%; padding: 12px; font-size: 20px; letter-spacing: 8px;
            text-align: center; background: #0d1117; border: 1px solid #30363d;
            border-radius: 8px; color: #fff; margin-bottom: 16px; box-sizing: border-box; }
    button { width: 100%; padding: 12px; background: #238636; border: none;
             border-radius: 8px; color: #fff; font-size: 16px; font-weight: 600;
             cursor: pointer; }
    button:hover { background: #2ea043; }
  </style>
</head>
<body>
  <div class="box">
    <h2>📱 Telegram Verification</h2>
    <p>A verification code has been sent to your Telegram account.<br/>Enter it below to connect the monitor.</p>
    <form method="POST" action="/auth/verify">
      <input type="text" name="code" placeholder="12345" maxlength="10" autofocus autocomplete="off"/>
      <button type="submit">Connect</button>
    </form>
  </div>
</body>
</html>"""


def _success_html() -> str:
    return """<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8"/>
  <meta http-equiv="refresh" content="3;url=/" />
  <title>Connected – Beirut Monitor</title>
  <style>
    body { background: #0d1117; color: #c9d1d9; font-family: system-ui, sans-serif;
           display: flex; align-items: center; justify-content: center; height: 100vh; margin: 0; }
    .box { background: #161b22; border: 1px solid #238636; border-radius: 12px;
           padding: 40px; max-width: 400px; width: 90%; text-align: center; }
    h2 { color: #3fb950; }
  </style>
</head>
<body>
  <div class="box">
    <h2>✅ Telegram Connected!</h2>
    <p>The monitor is now watching your channels.<br/>Redirecting to dashboard...</p>
  </div>
</body>
</html>"""


def _error_html(msg: str) -> str:
    return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8"/>
  <title>Error – Beirut Monitor</title>
  <style>
    body {{ background: #0d1117; color: #c9d1d9; font-family: system-ui, sans-serif;
           display: flex; align-items: center; justify-content: center; height: 100vh; margin: 0; }}
    .box {{ background: #161b22; border: 1px solid #f85149; border-radius: 12px;
           padding: 40px; max-width: 400px; width: 90%; text-align: center; }}
    h2 {{ color: #f85149; }}
    code {{ background: #0d1117; padding: 8px; border-radius: 4px; display: block;
            margin-top: 12px; font-size: 12px; word-break: break-all; }}
  </style>
</head>
<body>
  <div class="box">
    <h2>❌ Authentication Error</h2>
    <p>Something went wrong:</p>
    <code>{msg}</code>
    <p style="margin-top:16px;color:#8b949e">Try redeploying the app to start a fresh login.</p>
  </div>
</body>
</html>"""


def _waiting_html() -> str:
    return """<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8"/>
  <meta http-equiv="refresh" content="3;url=/auth" />
  <title>Connecting – Beirut Monitor</title>
  <style>
    body { background: #0d1117; color: #c9d1d9; font-family: system-ui, sans-serif;
           display: flex; align-items: center; justify-content: center; height: 100vh; margin: 0; }
    .box { background: #161b22; border: 1px solid #30363d; border-radius: 12px;
           padding: 40px; max-width: 400px; width: 90%; text-align: center; }
  </style>
</head>
<body>
  <div class="box">
    <h2>⏳ Connecting...</h2>
    <p>Verifying your code. Please wait...</p>
  </div>
</body>
</html>"""
