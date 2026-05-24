#!/usr/bin/env python3
"""
Kotipoti Dashboard Server
=========================
Serves the custom React dashboard for Freqtrade monitoring.
Proxies /api/v1/* to the Freqtrade REST API, handling Basic Auth
so credentials never appear in the browser.

Environment variables:
  FREQTRADE_API       Freqtrade base URL (default: http://localhost:8090)
  FREQTRADE_USERNAME  Freqtrade REST API username (default: freqtrader)
  FREQTRADE_PASSWORD  Freqtrade REST API password (required if auth is on)
  DASHBOARD_PORT      Port to serve this dashboard on (default: 5000)
"""

from flask import Flask, render_template_string, jsonify, request, Response
from flask_cors import CORS
import requests
import os
from pathlib import Path

app = Flask(__name__)
CORS(app)

# ── Config ────────────────────────────────────────────────────────────────────
FREQTRADE_API = os.environ.get("FREQTRADE_API", "http://localhost:8090")
FREQTRADE_USERNAME = os.environ.get("FREQTRADE_USERNAME", "freqtrader")
FREQTRADE_PASSWORD = os.environ.get("FREQTRADE_PASSWORD", "")
DASHBOARD_PORT = int(os.environ.get("DASHBOARD_PORT", 5000))

DASHBOARD_DIR = Path(__file__).parent
DASHBOARD_FILE = DASHBOARD_DIR / "custom-dashboard.jsx"

# Auth tuple for requests — only used if password is set
def _auth():
    if FREQTRADE_PASSWORD:
        return (FREQTRADE_USERNAME, FREQTRADE_PASSWORD)
    return None


# ── HTML template ─────────────────────────────────────────────────────────────
HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>KotipotiBot Dashboard</title>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; }}
    html, body {{ margin: 0; padding: 0; height: 100%; }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background: #0F172A;
      color: #F8FAFC;
    }}
    #root {{ width: 100%; min-height: 100vh; }}
  </style>
</head>
<body>
  <div id="root"></div>
  <script crossorigin src="https://unpkg.com/react@18/umd/react.production.min.js"></script>
  <script crossorigin src="https://unpkg.com/react-dom@18/umd/react-dom.production.min.js"></script>
  <script>
    {jsx_content}
    const root = ReactDOM.createRoot(document.getElementById('root'));
    root.render(React.createElement(KotipotiDashboard));
  </script>
</body>
</html>"""


def get_dashboard_html():
    jsx = DASHBOARD_FILE.read_text() if DASHBOARD_FILE.exists() else "// Dashboard file not found"
    return HTML_TEMPLATE.format(jsx_content=jsx)


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def dashboard():
    """Serve the dashboard HTML."""
    return get_dashboard_html(), 200, {"Content-Type": "text/html; charset=utf-8"}


@app.route("/api/v1/<path:endpoint>", methods=["GET", "POST", "DELETE"])
def proxy_freqtrade_api(endpoint):
    """
    Proxy to Freqtrade REST API with Basic Auth.
    Keeps credentials server-side — browser never sees them.
    """
    freqtrade_url = f"{FREQTRADE_API}/api/v1/{endpoint}"
    params = request.args.to_dict()
    auth = _auth()
    headers = {"Content-Type": "application/json"}

    try:
        if request.method == "GET":
            resp = requests.get(freqtrade_url, params=params, auth=auth, timeout=8, headers=headers)
        elif request.method == "POST":
            resp = requests.post(freqtrade_url, json=request.get_json(silent=True), params=params, auth=auth, timeout=8, headers=headers)
        else:  # DELETE
            resp = requests.delete(freqtrade_url, params=params, auth=auth, timeout=8, headers=headers)

        # Pass through the response as-is
        try:
            data = resp.json()
        except Exception:
            data = {"raw": resp.text}

        return jsonify(data), resp.status_code

    except requests.exceptions.ConnectionError:
        return jsonify({
            "error": "Cannot connect to Freqtrade API",
            "url": freqtrade_url,
            "hint": "Is freqtrade running? Check port 8090."
        }), 503
    except requests.exceptions.Timeout:
        return jsonify({"error": "Freqtrade API timed out", "url": freqtrade_url}), 504
    except Exception as e:
        return jsonify({"error": str(e), "endpoint": endpoint}), 500


@app.route("/health")
def health():
    """Health check — also pings Freqtrade to report its reachability."""
    ft_reachable = False
    ft_state = None
    try:
        r = requests.get(f"{FREQTRADE_API}/api/v1/ping", auth=_auth(), timeout=3)
        ft_reachable = r.status_code == 200
        # /api/v1/ping returns {"status": "pong"}
        ft_state = r.json().get("status")
    except Exception:
        pass

    return jsonify({
        "status": "ok",
        "freqtrade_api": FREQTRADE_API,
        "freqtrade_reachable": ft_reachable,
        "freqtrade_ping": ft_state,
        "dashboard_port": DASHBOARD_PORT,
        "auth_enabled": bool(FREQTRADE_PASSWORD),
    })


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    auth_status = "enabled" if FREQTRADE_PASSWORD else "disabled (no password set)"
    print(f"""
  ╔══════════════════════════════════════════════════════════════╗
  ║          KotipotiBot Dashboard Server                        ║
  ╠══════════════════════════════════════════════════════════════╣
  ║  Dashboard:    http://localhost:{DASHBOARD_PORT}
  ║  Health Check: http://localhost:{DASHBOARD_PORT}/health
  ║  Freqtrade:    {FREQTRADE_API}
  ║  Auth:         {auth_status}
  ╚══════════════════════════════════════════════════════════════╝
    """)

    app.run(
        host="0.0.0.0",
        port=DASHBOARD_PORT,
        debug=False,
        use_reloader=False,
    )
