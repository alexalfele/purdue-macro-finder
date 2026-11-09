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
    # Annealed plates that get the (expensive) two-serving deep polish.
    POLISH_TOP = 3

    # Meal plan constraints. A plate holds at most MAX_ITEMS different foods
    # and at least MIN_ITEMS servings in all.
    MIN_ITEMS = 2
    MAX_ITEMS = 5
    INITIAL_ITEMS = 4
    # How much of one food: about a normal helping. Purdue's serving units
    # range from a pork tenderloin to one slice of deli turkey (3.7 g
    # protein), so a fixed count can't work; instead a food may repeat until
    # it reaches MAX_PORTION_KCAL, or MAX_PORTION_CUPS / MAX_PORTION_OUNCES
    # when its serving label states a size (350 kcal of broccoli is 17 cups).
    # Any food may still be doubled (two chicken breasts, the way 60 g+
    # protein targets become reachable). Protein-dense foods (at least half
    # their calories from protein: sliced turkey, ham, chicken) repeat at most
    # MAX_SERVINGS_PER_FOOD times; anything else at most MAX_SERVINGS_OTHER,
    # so low-calorie produce can't become 32 tomato slices. Sauces, toppings
    # and spreads: one serving.
    MAX_SERVINGS = 2
    MAX_PORTION_KCAL = 350
    MAX_PORTION_CUPS = 2
    MAX_PORTION_OUNCES = 8
    MAX_SERVINGS_PER_FOOD = 20
    MAX_SERVINGS_OTHER = 6
    # Foods with less than this many grams of protein + carbs + fat (lettuce,
    # spices, peppers) can't meaningfully change a plate's macros; offering
    # them only fills plates with noise like "Arugula: 0 g / 1 g / 0 g".
    MIN_ITEM_MACROS = 2.0
    # Sauces, dressings, toppings and spreads per plate (one serving each).
    # Each also needs something from its station to go on; see
    # meal_finder_engine._make_plate_check.
    MAX_ADDONS = 2
    # A "serving" bigger than this many grams of any macro is a bulk container
    # in Purdue's data ("Bourbon Style Sauce, 4 x .5 Gal": 4,243 g carbs).
    MAX_SERVING_GRAMS = 150

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

    # When nothing is open, "Right now" plans the next meal: whatever starts
    # within this many minutes of the first opening, so at 6 am it's breakfast
    # at 7, not a court whose first meal is lunch at 11.
    NEXT_MEAL_WINDOW_MINUTES = 60

    # Validation limits
    MAX_MACRO_TARGET = 500  # grams
    MIN_MACRO_TARGET = 0

    # Dining courts
    DINING_COURTS = ["Wiley", "Earhart", "Windsor", "Ford", "Hillenbrand"]

    # Meal periods — these must match the names Purdue's API returns. Brunch is
    # weekend-only at Hillenbrand and replaces Breakfast there.
    MEAL_PERIODS = ["Breakfast", "Brunch", "Lunch", "Late Lunch", "Dinner"]

    # Dietary filters the API accepts. Diet filters keep only foods Purdue tags
    # that way (Vegetarian also accepts Vegan). Each "No ..." filter drops any
    # food tagged with one of its traits; wheat always contains gluten. These
    # come from Purdue's labels, which can be incomplete.
    DIET_FILTERS = ("Vegetarian", "Vegan")
    AVOID_FILTERS = {
        "No Gluten": ("Gluten", "Wheat", "Contains Gluten"),
        "No Milk": ("Milk",),
        "No Eggs": ("Eggs",),
        "No Soy": ("Soy",),
        "No Peanuts": ("Peanuts",),
        "No Tree Nuts": ("Tree Nuts",),
        "No Fish": ("Fish",),
        "No Shellfish": ("Shellfish",),
        "No Sesame": ("Sesame",),
        "No Coconut": ("Coconut",),
    }

    # Purdue API
    PURDUE_API_URL = "https://api.hfs.purdue.edu/menus/v3/GraphQL"

    # Rate limiting
    RATE_LIMIT_PER_DAY = 200
    RATE_LIMIT_PER_HOUR = 50
    RATE_LIMIT_PER_MINUTE = 20
