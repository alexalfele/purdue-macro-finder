import os
from datetime import datetime
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from meal_finder_engine import MealFinder
from config import Config  # Import the Config class
import threading
import time
import requests
import logging

# --- 1. SETUP THE FLASK APP ---
# Serve the bundled frontend (index.html) from this same directory so we don't
# need a separate static server or hardcoded API_BASE_URL on the client.
APP_ROOT = os.path.dirname(os.path.abspath(__file__))
ENV_FILE = os.path.join(APP_ROOT, ".env")


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

# static_folder=None: do NOT expose the project directory over HTTP. The three
# HTML pages are served by explicit routes below; there are no other static
# assets (CSS/JS are inline, Chart.js is loaded from a CDN).
app = Flask(__name__, static_folder=None)
# The API returns only public dining data, so cross-origin reads are fine, but
# scope CORS to /api/* rather than opening the whole app.
CORS(app, resources={r"/api/*": {"origins": "*"}})
logging.basicConfig(level=logging.INFO)


@app.after_request
def _security_headers(resp):
    """Baseline hardening headers applied to every response."""
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    # Harmless over plain HTTP (browsers ignore it); enforced once on Render's HTTPS.
    resp.headers.setdefault(
        "Strict-Transport-Security", "max-age=63072000; includeSubDomains"
    )
    resp.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "frame-ancestors 'none'; "
        "base-uri 'none'; "
        "form-action 'self'",
    )
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

def validate_targets(targets):
    """Validates the macro targets dictionary."""
    required_macros = ['p', 'c', 'f']
    for macro in required_macros:
        if macro not in targets:
            return False, f"Missing required macro: {macro}"
        
        value = targets[macro]
        try:
            val = float(value)
            if not Config.MIN_MACRO_TARGET <= val <= Config.MAX_MACRO_TARGET:
                return False, f"Macro {macro} ({val}) is out of range. Must be between {Config.MIN_MACRO_TARGET} and {Config.MAX_MACRO_TARGET}."
        except (ValueError, TypeError):
            return False, f"Macro {macro} must be a valid number."
            
    return True, None

def validate_meal_periods(meal_periods):
    """Validates the list of meal periods."""
    if not meal_periods or not isinstance(meal_periods, list):
        return False, "Please select at least one meal period."
    
    for period in meal_periods:
        if period not in Config.MEAL_PERIODS:
            return False, f"Invalid meal period: {period}"
            
    return True, None

# --- 6. ROUTES ---
# Static pages and the health probe are exempt from rate limiting so ordinary
# browsing isn't throttled; the API endpoints below keep their strict limits.
@app.route("/")
@limiter.exempt
def index():
    """Serves the single-page frontend."""
    return send_from_directory(APP_ROOT, "index.html")

@app.route("/terms")
@limiter.exempt
def terms():
    """Serves the Terms of Service page."""
    return send_from_directory(APP_ROOT, "terms.html")

@app.route("/privacy")
@limiter.exempt
def privacy():
    """Serves the Privacy Policy page."""
    return send_from_directory(APP_ROOT, "privacy.html")

@app.route("/api/health")
@limiter.exempt
def health_check():
    """Lightweight liveness probe for the keep-alive pinger."""
    return jsonify({"status": "healthy", "message": "Purdue Macro Finder API is running."})


# --- 7. API ENDPOINTS ---
@app.route("/api/find_meal", methods=["POST"])
@limiter.limit(limit_minute)
@limiter.limit(limit_hour)
@limiter.limit(limit_day)
def api_find_meal():
    engine = get_engine()
    try:
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"error": "Request body must be a JSON object."}), 400
        targets = data.get('targets', {})
        meal_periods = data.get('meal_periods', [])
        dietary_filters = data.get('dietary_filters', {})
        exclusion_list = data.get('exclusion_list', [])
        raw_date = data.get('date')

        valid, error = validate_targets(targets)
        if not valid:
            return jsonify({"error": error}), 400

        valid, error = validate_meal_periods(meal_periods)
        if not valid:
            return jsonify({"error": error}), 400

        try:
            date_str = engine.normalize_date(raw_date)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400

        # On a cold start the menu for this date isn't in memory yet. Rather than
        # blocking the request for the full Purdue fetch (and risking the
        # client's timeout), load in the background and tell the client to retry.
        if not engine.has_data(date_str):
            engine.ensure_loaded_async(date_str)
            return jsonify({
                "error": "Menu data is still loading, please retry in a few seconds."
            }), 503

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

@app.route("/api/featured", methods=["GET"])
@limiter.limit(limit_minute)
@limiter.limit(limit_hour)
@limiter.limit(limit_day)
def api_featured():
    """Returns the featured plate for what's being served right now (or the next
    meal hint if it's outside dining hours). Optional ?date=YYYY-MM-DD."""
    engine = get_engine()
    raw_date = request.args.get("date")
    try:
        date_str = engine.normalize_date(raw_date)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    try:
        result = engine.featured_plate(date_str=date_str)
        return jsonify(result)
    except Exception:
        app.logger.exception("Error in /api/featured")
        return jsonify({"error": "An internal error occurred."}), 500


# --- 8. START THE SERVER (local dev only) ---
# In production, gunicorn imports `app` directly — this block does not run.
if __name__ == "__main__":
    app.logger.info("Starting Flask development server...")
    get_engine()
    # Use 0.0.0.0 to be accessible on the network
    app.run(host='0.0.0.0', debug=False, port=int(os.environ.get("PORT", 5000)))
