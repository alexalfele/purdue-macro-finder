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


def _write_dotenv_kv(path, key, value):
    """Idempotently update or insert KEY=VALUE in a .env-style file. Atomic write,
    chmods to 0600. Returns True on success, False on filesystem failure."""
    lines = []
    if os.path.exists(path):
        try:
            with open(path) as f:
                lines = f.readlines()
        except OSError:
            lines = []

    new_line = f"{key}={value}\n"
    found = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith(f"{key}=") or stripped.startswith(f"{key} ="):
            lines[i] = new_line
            found = True
            break
    if not found:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] = lines[-1] + "\n"
        lines.append(new_line)

    tmp = path + ".tmp"
    try:
        with open(tmp, "w") as f:
            f.writelines(lines)
        os.replace(tmp, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return True
    except OSError as e:
        logging.warning(f"Could not write {path}: {e}")
        return False


_load_dotenv(ENV_FILE)

app = Flask(__name__, static_folder=APP_ROOT, static_url_path="")
CORS(app)
logging.basicConfig(level=logging.INFO)

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
@app.route("/")
def index():
    """Serves the single-page frontend."""
    return send_from_directory(APP_ROOT, "index.html")

@app.route("/api/health")
def health_check():
    """Lightweight liveness probe for the keep-alive pinger."""
    return jsonify({"status": "healthy", "message": "Purdue Macro Finder API is running."})


@app.route("/api/settings", methods=["GET"])
def api_settings_get():
    """Returns whether AI is configured. Never echoes the key — only the last 4 chars."""
    engine = get_engine()
    return jsonify({
        "ai_configured": engine.is_ai_configured(),
        "api_key_last4": engine.api_key_masked(),
    })


@app.route("/api/settings/api_key", methods=["POST"])
@limiter.limit("5 per minute")
def api_settings_set_key():
    """Validates the key with a tiny live Gemini call, then persists to .env.

    Note: in a multi-tenant deployment this endpoint would need auth. For this
    project it's intended for the owner's local/personal use; in production set
    the key via the platform's env var dashboard instead.
    """
    engine = get_engine()
    data = request.json or {}
    key = (data.get("api_key") or "").strip()

    if not key:
        return jsonify({"error": "api_key is required"}), 400
    if len(key) < 20 or len(key) > 200:
        return jsonify({"error": "That doesn't look like a valid API key (wrong length)."}), 400

    ok, err = engine.set_api_key(key)
    if not ok:
        return jsonify({"error": err}), 400

    persisted = _write_dotenv_kv(ENV_FILE, "GEMINI_API_KEY", key)
    return jsonify({
        "ok": True,
        "ai_configured": True,
        "api_key_last4": engine.api_key_masked(),
        "persisted": persisted,
        "note": None if persisted else (
            "Key is active for this server but couldn't be written to .env "
            "(filesystem read-only?). It will be lost on restart."
        ),
    })

# --- 7. API ENDPOINTS ---
@app.route("/api/find_meal", methods=["POST"])
@limiter.limit(limit_minute)
@limiter.limit(limit_hour)
@limiter.limit(limit_day)
def api_find_meal():
    engine = get_engine()
    try:
        data = request.json or {}
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

    except Exception as e:
        app.logger.error(f"Error in /api/find_meal: {e}")
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
    except Exception as e:
        app.logger.error(f"Error in /api/featured: {e}")
        return jsonify({"error": "An internal error occurred."}), 500


@app.route("/api/suggest_meal", methods=["POST"])
@limiter.limit(limit_minute)
@limiter.limit(limit_hour)
@limiter.limit(limit_day)
def api_suggest_meal():
    engine = get_engine()
    try:
        data = request.json or {}
        goal = data.get('goal')
        raw_date = data.get('date')

        if not goal or len(goal) < 5:
            return jsonify({"error": "A descriptive goal is required."}), 400

        try:
            date_str = engine.normalize_date(raw_date)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400

        app.logger.info(f"AI Goal received: '{goal}' (date={date_str})")
        ai_result = engine.get_macros_from_ai(goal, date_str=date_str)

        if ai_result.get("error"):
            return jsonify({"error": ai_result.get("error")}), 500

        targets = ai_result.get("targets")
        ai_explanation = ai_result.get("explanation", "AI analyzed your goal.")
        ai_meal_periods = ai_result.get("meal_periods") or ["Lunch", "Dinner"]
        ai_dietary_filters = ai_result.get("dietary_filters") or {}

        app.logger.info(
            f"AI plan: targets={targets} meals={ai_meal_periods} filters={ai_dietary_filters}"
        )

        valid, error = validate_targets(targets)
        if not valid:
            app.logger.error(f"AI returned invalid targets: {error}")
            return jsonify({"error": "The AI provided an invalid target. Please rephrase your goal."}), 500

        valid, error = validate_meal_periods(ai_meal_periods)
        if not valid:
            app.logger.warning(f"AI returned invalid meal periods, falling back: {error}")
            ai_meal_periods = ["Lunch", "Dinner"]

        optimized_meal = engine.find_best_meal(
            targets=targets,
            meal_periods_to_check=ai_meal_periods,
            exclusion_list=[],
            dietary_filters=ai_dietary_filters,
            date_str=date_str,
        )

        if optimized_meal is None:
            return jsonify({
                "error": (
                    f"No meal plan found for {date_str}. AI set targets: "
                    f"P:{targets['p']} C:{targets['c']} F:{targets['f']} "
                    f"across {', '.join(ai_meal_periods)}. Try a different goal or date."
                )
            }), 404

        optimized_meal['ai_explanation'] = (
            f"For your goal I set targets of P:{targets['p']}g, C:{targets['c']}g, "
            f"F:{targets['f']}g across {', '.join(ai_meal_periods)}. {ai_explanation}"
        )
        optimized_meal['ai_targets'] = targets
        optimized_meal['ai_meal_periods'] = ai_meal_periods
        optimized_meal['ai_dietary_filters'] = ai_dietary_filters

        return jsonify(optimized_meal)

    except Exception as e:
        app.logger.error(f"Error in /api/suggest_meal: {e}")
        return jsonify({"error": "An internal error occurred."}), 500

# --- 8. START THE SERVER (local dev only) ---
# In production, gunicorn imports `app` directly — this block does not run.
if __name__ == "__main__":
    app.logger.info("Starting Flask development server...")
    get_engine()
    # Use 0.0.0.0 to be accessible on the network
    app.run(host='0.0.0.0', debug=False, port=int(os.environ.get("PORT", 5000)))
