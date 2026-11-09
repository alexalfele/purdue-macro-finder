import gzip
import hashlib
import mimetypes
import os
from datetime import datetime
from flask import Flask, request, jsonify, make_response, abort
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from meal_finder_engine import MealFinder
from config import Config
import threading
import time
import requests
import logging

# --- 1. SETUP THE FLASK APP ---
APP_ROOT = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(APP_ROOT, "static")
ENV_FILE = os.path.join(APP_ROOT, ".env")

# The only static files served. Everything else in the project directory
# (source, .env, caches) stays unreachable over HTTP.
STATIC_FILES = {
    "app.css", "app.js", "legal.css", "chart.umd.min.js",
    "manifest.webmanifest", "favicon.svg", "icon-192.png", "icon-512.png",
    "apple-touch-icon.png",
}
# Types Python's mimetypes may not know; text types get a charset.
_CONTENT_TYPES = {".webmanifest": "application/manifest+json", ".svg": "image/svg+xml"}
_TEXT_TYPES = ("text/", "application/javascript", "application/manifest+json", "image/svg+xml")


def _load_dotenv(path):
    """Tiny no-deps .env loader. Pre-existing env vars win (so Render's dashboard
    settings always override the file)."""
    if not os.path.exists(path):
        return
    try:
        with open(path) as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key, val = key.strip(), val.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = val
    except OSError as e:
        logging.warning(f"Could not read {path}: {e}")


_load_dotenv(ENV_FILE)

# static_folder=None: do NOT expose the project directory over HTTP. Pages and
# the allow-listed static files are served by explicit routes below.
app = Flask(__name__, static_folder=None)
# The API returns only public dining data, so cross-origin reads are fine, but
# scope CORS to /api/* rather than opening the whole app.
CORS(app, resources={r"/api/*": {"origins": "*"}})
logging.basicConfig(level=logging.INFO)

# Every script and style is a file served by this app (Chart.js included), so
# the policy needs neither 'unsafe-inline' nor any third-party origin.
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self'; "
    "img-src 'self' data:; "
    "connect-src 'self'; "
    "object-src 'none'; "
    "frame-ancestors 'none'; "
    "base-uri 'none'; "
    "form-action 'self'"
)


@app.after_request
def _security_headers(resp):
    """Baseline hardening headers applied to every response."""
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    resp.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
    resp.headers.setdefault(
        "Permissions-Policy",
        "camera=(), microphone=(), geolocation=(), payment=(), usb=(), interest-cohort=()",
    )
    # Harmless over plain HTTP (browsers ignore it); enforced once on Render's HTTPS.
    resp.headers.setdefault(
        "Strict-Transport-Security", "max-age=63072000; includeSubDomains"
    )
    resp.headers.setdefault("Content-Security-Policy", CONTENT_SECURITY_POLICY)
    if request.path.startswith("/api/"):
        resp.headers.setdefault("Cache-Control", "no-store")
    return resp

# --- 2. CONFIGURE RATE LIMITING ---
limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=["100 per day", "20 per hour"], # Fallback
    storage_uri="memory://",
)

# Apply limits from config.py
limit_minute = f"{Config.RATE_LIMIT_PER_MINUTE} per minute"
limit_hour = f"{Config.RATE_LIMIT_PER_HOUR} per hour"
limit_day = f"{Config.RATE_LIMIT_PER_DAY} per day"

# --- 3. KEEP-ALIVE CONFIGURATION ---
KEEP_ALIVE_URL = os.environ.get("RENDER_EXTERNAL_URL", "")
KEEP_ALIVE_INTERVAL = 840  # 14 minutes
ENABLE_KEEP_ALIVE = os.environ.get("ENABLE_KEEP_ALIVE", "true").lower() == "true"

def keep_alive_ping():
    """Pings the server periodically to prevent spin-down"""
    while True:
        time.sleep(KEEP_ALIVE_INTERVAL)
        if KEEP_ALIVE_URL:
            try:
                requests.get(f"{KEEP_ALIVE_URL}/api/health", timeout=10)
                app.logger.info(f"[{datetime.now().strftime('%H:%M:%S')}] Keep-alive ping successful")
            except Exception as e:
                app.logger.warning(f"[{datetime.now().strftime('%H:%M:%S')}] Keep-alive ping failed: {e}")


def _start_keep_alive_thread():
    """Start the keep-alive pinger if enabled. Safe to call once at import time."""
    if ENABLE_KEEP_ALIVE and KEEP_ALIVE_URL:
        t = threading.Thread(target=keep_alive_ping, daemon=True, name="KeepAlive")
        t.start()
        app.logger.info(f"Keep-alive thread started (pinging every {KEEP_ALIVE_INTERVAL}s)")
    else:
        app.logger.info("Keep-alive disabled or RENDER_EXTERNAL_URL not available")


# Start keep-alive at module import time so it runs under gunicorn on Render,
# not only under `python app.py`.
_start_keep_alive_thread()

# --- 4. LAZY INITIALIZATION SETUP ---
meal_finder_engine = None
engine_lock = threading.Lock()


def get_engine():
    """Initializes and returns the MealFinder engine (thread-safe)."""
    global meal_finder_engine
    if meal_finder_engine:
        return meal_finder_engine

    with engine_lock:
        if meal_finder_engine:
            return meal_finder_engine

        app.logger.info("FIRST REQUEST: Initializing MealFinder engine...")
        meal_finder_engine = MealFinder()
        app.logger.info("FIRST REQUEST: Starting background data loaders...")
        meal_finder_engine.start_background_loaders()
        app.logger.info("FIRST REQUEST: Initialization complete. Engine is live.")

        return meal_finder_engine

# --- 5. INPUT VALIDATION FUNCTIONS ---

MAX_EXCLUSIONS = 50
MAX_EXCLUSION_LENGTH = 80
MACRO_NAMES = {"p": "Protein", "c": "Carbs", "f": "Fat"}
MODES = ("plan", "now")


def validate_targets(targets):
    """Validates the macro targets dictionary."""
    if not isinstance(targets, dict):
        return False, "Targets must be an object with p, c, and f."
    for macro, name in MACRO_NAMES.items():
        if macro not in targets:
            return False, f"Missing required macro: {macro}"
        try:
            val = float(targets[macro])
        except (ValueError, TypeError):
            return False, f"{name} must be a number."
        if not Config.MIN_MACRO_TARGET <= val <= Config.MAX_MACRO_TARGET:
            return False, (
                f"{name} ({val:g} g) is out of range. Use "
                f"{Config.MIN_MACRO_TARGET} to {Config.MAX_MACRO_TARGET} g."
            )
    return True, None

def validate_meal_periods(meal_periods):
    """Validates the list of meal periods."""
    if not meal_periods or not isinstance(meal_periods, list):
        return False, "Please select at least one meal period."

    for period in meal_periods:
        if not isinstance(period, str) or period not in Config.MEAL_PERIODS:
            return False, f"Invalid meal period: {period}"

    return True, None

DIETARY_FILTERS = frozenset(Config.DIET_FILTERS) | frozenset(Config.AVOID_FILTERS)


def validate_dietary_filters(dietary_filters):
    """Dietary filters must be an object like {"Vegan": true, "No Milk": true}
    using only known filter names, so a typo can't silently filter nothing."""
    if not isinstance(dietary_filters, dict):
        return False, "Dietary filters must be an object."
    for name, value in dietary_filters.items():
        if name not in DIETARY_FILTERS:
            return False, f"Unknown dietary filter: {str(name)[:40]}"
        if not isinstance(value, bool):
            return False, "Dietary filter values must be true or false."
    return True, None

def validate_exclusion_list(exclusion_list):
    """Exclusions must be a short list of short strings. A bare string would be
    iterated letter by letter and exclude nearly every food."""
    if not isinstance(exclusion_list, list):
        return False, "Exclusion list must be a list of food names."
    if len(exclusion_list) > MAX_EXCLUSIONS:
        return False, f"Exclude at most {MAX_EXCLUSIONS} foods."
    for term in exclusion_list:
        if not isinstance(term, str) or len(term) > MAX_EXCLUSION_LENGTH:
            return False, f"Each excluded food must be text of at most {MAX_EXCLUSION_LENGTH} characters."
    return True, None

# --- 6. PAGES AND STATIC FILES ---
# Static and page routes are exempt from rate limiting so ordinary browsing
# isn't throttled; the API endpoints below keep their strict limits.

def _asset_version():
    """Short content hash of the static files, appended to their URLs so a
    deploy busts browser caches while unchanged files stay cached for a year."""
    digest = hashlib.sha256()
    for name in sorted(STATIC_FILES):
        with open(os.path.join(STATIC_DIR, name), "rb") as f:
            digest.update(f.read())
    return digest.hexdigest()[:12]


def _accepts_gzip():
    return "gzip" in request.headers.get("Accept-Encoding", "").lower()


def _respond(body, content_type, cache_control, gzipped=None, compress=True):
    """Bytes response, gzip-compressed when the client accepts it.

    `gzipped` lets callers pass a precomputed compressed body; compress=False
    sends already-compressed formats (PNG) as they are.
    """
    if compress and _accepts_gzip() and len(body) > 1024:
        resp = make_response(gzipped if gzipped is not None else gzip.compress(body))
        resp.headers["Content-Encoding"] = "gzip"
    else:
        resp = make_response(body)
    resp.headers["Content-Type"] = content_type
    resp.headers["Cache-Control"] = cache_control
    resp.headers["Vary"] = "Accept-Encoding"
    return resp


def _page(name):
    """Serve an HTML page with its asset URLs versioned. Always revalidated."""
    with open(os.path.join(APP_ROOT, name), encoding="utf-8") as f:
        html = f.read().replace("__ASSET_VERSION__", _asset_version())
    return _respond(html.encode("utf-8"), "text/html; charset=utf-8", "no-cache")


# name -> (mtime, raw bytes, gzipped bytes); compressed once, not per request.
_static_cache = {}


def _static_bytes(name):
    path = os.path.join(STATIC_DIR, name)
    mtime = os.path.getmtime(path)
    cached = _static_cache.get(name)
    if cached is None or cached[0] != mtime:
        with open(path, "rb") as f:
            raw = f.read()
        cached = (mtime, raw, gzip.compress(raw, compresslevel=9))
        _static_cache[name] = cached
    return cached[1], cached[2]


@app.route("/")
@limiter.exempt
def index():
    """Serves the single-page frontend."""
    return _page("index.html")

@app.route("/terms")
@limiter.exempt
def terms():
    """Serves the Terms of Service page."""
    return _page("terms.html")

@app.route("/privacy")
@limiter.exempt
def privacy():
    """Serves the Privacy Policy page."""
    return _page("privacy.html")

@app.route("/static/<name>")
@limiter.exempt
def static_asset(name):
    """Serves the allow-listed files. Pages link them with a content hash
    (?v=...), so those URLs are cached as immutable; bare URLs, such as the
    icons a manifest names, are cached for a day so a redesign still lands."""
    if name not in STATIC_FILES:
        abort(404)
    raw, compressed = _static_bytes(name)
    mime = (_CONTENT_TYPES.get(os.path.splitext(name)[1])
            or mimetypes.guess_type(name)[0] or "application/octet-stream")
    if mime.startswith(_TEXT_TYPES):
        mime += "; charset=utf-8"
    else:
        compressed = None  # PNGs are already compressed
    cache = "public, max-age=31536000, immutable" if "v" in request.args else "public, max-age=86400"
    return _respond(raw, mime, cache, gzipped=compressed, compress=compressed is not None)

@app.route("/api/health")
@limiter.exempt
def health_check():
    """Lightweight liveness probe for the keep-alive pinger."""
    return jsonify({"status": "healthy", "message": "Purdue Macro Finder API is running."})


# --- 7. API ENDPOINTS ---
def _still_loading():
    """503 tells the page to retry shortly while menus load in the background."""
    return jsonify({"error": "Menu data is still loading, please retry in a few seconds."}), 503



@app.route("/api/find_meal", methods=["POST"])
@limiter.limit(limit_minute)
@limiter.limit(limit_hour)
@limiter.limit(limit_day)
def api_find_meal():
    """Find a plate that matches the targets.

    mode "plan" (default): for `date` and `meal_periods`.
    mode "now": from whatever is being served right now at Purdue; `date` and
    `meal_periods` are ignored.
    """
    engine = get_engine()
    try:
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"error": "Request body must be a JSON object."}), 400
        mode = data.get('mode', 'plan')
        targets = data.get('targets', {})
        meal_periods = data.get('meal_periods', [])
        dietary_filters = data.get('dietary_filters', {})
        exclusion_list = data.get('exclusion_list', [])

        if mode not in MODES:
            return jsonify({"error": f"Mode must be one of {', '.join(MODES)}."}), 400
        for valid, error in (
            validate_targets(targets),
            validate_dietary_filters(dietary_filters),
            validate_exclusion_list(exclusion_list),
        ):
            if not valid:
                return jsonify({"error": error}), 400

        if mode == "plan":
            valid, error = validate_meal_periods(meal_periods)
            if not valid:
                return jsonify({"error": error}), 400
            try:
                date_str = engine.normalize_date(data.get('date'))
            except ValueError as e:
                return jsonify({"error": str(e)}), 400
        else:
            date_str = engine.normalize_date(None)  # today, in Purdue time

        # On a cold start the menu for this date isn't in memory yet. Rather than
        # blocking one of gunicorn's few threads for the full Purdue fetch (and
        # risking the client's timeout), load in the background and tell the
        # client to retry.
        if not engine.has_data(date_str):
            engine.ensure_loaded_async(date_str)
            return _still_loading()

        if mode == "now":
            result = engine.find_meal_now(targets, exclusion_list, dietary_filters)
            if result is None:
                return jsonify({
                    "error": (
                        "No food on the menu fits your dietary filters and "
                        "exclusions. Try loosening them."
                    )
                }), 404
            if result.get("loading"):  # nothing open; tomorrow's menu is loading
                return _still_loading()
            return jsonify(result)

        result = engine.find_best_meal(
            targets,
            meal_periods,
            exclusion_list,
            dietary_filters,
            date_str=date_str,
        )
        if result is None:
            return jsonify({
                "error": (
                    f"No meal plan found for {date_str}. "
                    "Try a different date, broader meal periods, or fewer dietary filters."
                )
            }), 404
        return jsonify(result)

    except Exception:
        app.logger.exception("Error in /api/find_meal")
        return jsonify({"error": "An internal error occurred."}), 500


# --- 8. START THE SERVER (local dev only) ---
# In production, gunicorn imports `app` directly — this block does not run.
if __name__ == "__main__":
    app.logger.info("Starting Flask development server...")
    get_engine()
    # Use 0.0.0.0 to be accessible on the network
    app.run(host='0.0.0.0', debug=False, port=int(os.environ.get("PORT", 5000)))
