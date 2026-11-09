"""
Configuration constants for Purdue Macro Finder
"""

class Config:
    """Application configuration and constants"""

    # Purdue's local time. Servers (Render included) usually run on UTC, so
    # "today" and "right now" must be computed in this zone, not the process
    # clock, or every evening after 7 pm the app thinks it's tomorrow.
    TIMEZONE = "America/Indiana/Indianapolis"

    # Optimization parameters
    WEIGHTS = {'p': 3.0, 'c': 1.0, 'f': 1.5}
    PENALTIES = {'under_p': 2, 'over_c': 1.2, 'over_f': 3}
    # Per (court, meal) pool: RESTARTS simulated-annealing runs (from greedy
    # seeds), each starting at a temperature derived from its seed's score and
    # cooling geometrically to FINAL_TEMP over ITERATIONS steps, then finished
    # with a local search over every 1- and 2-serving change. The best plate
    # wins. Measured: the exact optimum on 92% of random menus and otherwise
    # within ~5 score points (a few grams on one macro), at ~10 ms per pool.
    ITERATIONS = 1000
    FINAL_TEMP = 0.05
    RESTARTS = 4

    # Meal plan constraints. Item limits count servings, so "2 servings of
    # chicken + rice" is 3 items. Two servings of one food is how high-protein
    # targets (60 g+) become reachable in a single dining-court meal.
    MIN_ITEMS = 2
    MAX_ITEMS = 5
    INITIAL_ITEMS = 4
    MAX_SERVINGS = 2
    # Foods with less than this many grams of protein + carbs + fat (lettuce,
    # spices, peppers) can't meaningfully change a plate's macros; offering
    # them only fills plates with noise like "Arugula: 0 g / 1 g / 0 g".
    MIN_ITEM_MACROS = 2.0

    # API settings
    API_TIMEOUT = 10

    # Cache settings
    CACHE_PREFIX_MENU = "menu_cache_"
    # Bump when the GraphQL query or parsing changes, so caches written by an
    # older version (missing fields like station components or meal hours) are
    # refetched instead of silently reused.
    CACHE_VERSION = 2
    # How long a cached menu for today-or-later stays trusted. Purdue keeps
    # editing the current day's menu and publishes future days late, so a stale
    # snapshot must be re-fetched. Past dates never expire.
    CACHE_TTL_HOURS = 12
    # A load where a court failed to fetch, or where nothing was published yet,
    # is retried after this many minutes instead of waiting out the full TTL.
    INCOMPLETE_RETRY_MINUTES = 10

    # Validation limits
    MAX_MACRO_TARGET = 500  # grams
    MIN_MACRO_TARGET = 0

    # Dining courts
    DINING_COURTS = ["Wiley", "Earhart", "Windsor", "Ford", "Hillenbrand"]

    # Meal periods — these must match the names Purdue's API returns. Brunch is
    # weekend-only at Hillenbrand and replaces Breakfast there.
    MEAL_PERIODS = ["Breakfast", "Brunch", "Lunch", "Late Lunch", "Dinner"]

    # Purdue API
    PURDUE_API_URL = "https://api.hfs.purdue.edu/menus/v3/GraphQL"

    # Rate limiting
    RATE_LIMIT_PER_DAY = 200
    RATE_LIMIT_PER_HOUR = 50
    RATE_LIMIT_PER_MINUTE = 20
