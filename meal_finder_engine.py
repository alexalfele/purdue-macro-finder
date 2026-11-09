import bisect
import threading
import requests
import json
import re
import random
import math
import os
import logging
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
from zoneinfo import ZoneInfo
from config import Config

logger = logging.getLogger(__name__)

PURDUE_TZ = ZoneInfo(Config.TIMEZONE)

# Cache files live next to this module, not in whatever directory the process
# happened to be started from.
_MODULE_DIR = os.path.dirname(os.path.abspath(__file__))

# Typical hours (24h clock), used only when Purdue's API omits a meal's start
# and end time. Real hours vary by court and day and come from the API.
_FALLBACK_HOURS = [
    # (meal, start, end, weekend_only)
    ("Breakfast", 7, 10, False),
    ("Brunch", 10, 15, True),
    ("Lunch", 11, 14, False),
    ("Late Lunch", 14, 17, False),
    ("Dinner", 17, 21, False),
]

# Traits that mean an item is not gluten-free. Purdue tags gluten as "Gluten"
# and wheat separately as "Wheat"; wheat always contains gluten.
_GLUTEN_TRAITS = {"Gluten", "Wheat", "Contains Gluten"}

_MACRO_FACTS = {"Protein": "p", "Total Carbohydrate": "c", "Total fat": "f", "Calories": "cal"}


def _format_clock(hour, minute=0):
    """17, 0 -> '5 pm'; 10, 30 -> '10:30 am'; 12, 0 -> '12 pm'."""
    suffix = "am" if hour < 12 else "pm"
    h12 = hour % 12 or 12
    return f"{h12} {suffix}" if not minute else f"{h12}:{minute:02d} {suffix}"


def _hours_payload(start, end):
    """JSON-friendly meal hours, or None when Purdue didn't publish them."""
    if not start or not end:
        return None
    first = _format_clock(start.hour, start.minute)
    last = _format_clock(end.hour, end.minute)
    # Match the page's wording: "5 to 9 pm", but "11 am to 2 pm".
    if (start.hour < 12) == (end.hour < 12):
        first = first.rsplit(" ", 1)[0]
    return {
        "start": start.strftime("%H:%M"),
        "end": end.strftime("%H:%M"),
        "label": f"{first} to {last}",
    }


class MealFinder:
    """Backend for fetching Purdue dining-court data and finding optimal meal plans.

    Data is keyed by date (YYYY-MM-DD). Each date has its own in-memory snapshot
    and disk cache file, loaded lazily on first request.
    """

    # Composite entries such as "Salad Bar" or "La Fonda Station" carry no
    # nutrition of their own; the foods are listed as their `components`, so the
    # query asks for those (two levels deep, for safety).
    GRAPHQL_QUERY = """
    fragment Food on ItemAppearance {
      displayName
      item { traits { name } nutritionFacts { name value label } }
    }
    query GetMenu($courtName: String!, $date: Date!) {
      diningCourtByName(name: $courtName) {
        name
        dailyMenu(date: $date) {
          meals {
            name status startTime endTime
            stations {
              name
              items { ...Food components { ...Food components { ...Food } } }
            }
          }
        }
      }
    }
    """

    def __init__(self):
        self.url = Config.PURDUE_API_URL
        self.headers = {"Content-Type": "application/json"}
        self.dining_courts = Config.DINING_COURTS

        # Per-date menu snapshots: date_str -> {master, hours, loaded_at, complete}
        self.menu_by_date = {}

        # One lock for all menu data. Coarse but adequate.
        self.data_lock = threading.Lock()

        # Per-date load locks so two threads asking for the same date don't both
        # hit the Purdue API. Created on demand.
        self._date_load_locks = {}
        self._load_locks_meta = threading.Lock()
        # Dates with a background load in flight, so we start at most one each.
        self._loading = set()

    # --- time helpers (always Purdue local time) ---

    @staticmethod
    def _now():
        """Current time at Purdue, timezone-aware."""
        return datetime.now(PURDUE_TZ)

    @staticmethod
    def _today():
        return MealFinder._now().strftime("%Y-%m-%d")

    @staticmethod
    def _as_purdue_time(value):
        """Parse an ISO timestamp (or take a datetime) and express it in Purdue
        time. Naive values are assumed to already be Purdue time. Returns None
        for anything unparseable."""
        if value is None:
            return None
        if isinstance(value, str):
            try:
                value = datetime.fromisoformat(value)
            except ValueError:
                return None
        if not isinstance(value, datetime):
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=PURDUE_TZ)
        return value.astimezone(PURDUE_TZ)

    @staticmethod
    def normalize_date(date_str):
        """Returns a valid YYYY-MM-DD string, defaulting to today.

        Raises ValueError if the input is malformed or unreasonable.
        """
        if not date_str:
            return MealFinder._today()
        try:
            parsed = datetime.strptime(str(date_str), "%Y-%m-%d").date()
        except ValueError as exc:
            raise ValueError(f"Date must be YYYY-MM-DD, got {date_str!r}") from exc
        today = MealFinder._now().date()
        # Allow up to a year back and 30 days forward — anything outside that
        # window is almost certainly a mistake or stale state from the client.
        if parsed < today - timedelta(days=365) or parsed > today + timedelta(days=30):
            raise ValueError(
                f"Date {parsed} is outside the supported window "
                f"(1 year back to 30 days forward)."
            )
        return parsed.strftime("%Y-%m-%d")

    def _menu_cache_path(self, date_str):
        return os.path.join(_MODULE_DIR, f"{Config.CACHE_PREFIX_MENU}{date_str}.json")

    # --- numeric / scoring ---

    def _get_numeric_value(self, label_str):
        """Extracts a number from a label like '15g' -> 15.0.

        Tolerates messy labels ('< 1 g', 'N/A', '1.2.3 oz') without raising.
        """
        if not label_str:
            return 0.0
        # Match a single well-formed number so inputs like "1.2.3" don't blow up
        # float(). Leading number wins ("2.5g (per cup)" -> 2.5).
        match = re.search(r"\d+(?:\.\d+)?", str(label_str))
        if not match:
            return 0.0
        try:
            return float(match.group(0))
        except ValueError:
            return 0.0

    def _nutrient(self, fact):
        """Prefer Purdue's precise numeric value; fall back to the rounded label."""
        value = fact.get("value")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return round(float(value), 1)
        return self._get_numeric_value(fact.get("label"))

    @staticmethod
    def _make_scorer(targets, weights, penalties):
        """Returns score(p, c, f) -> float; lower is better.

        Penalises under-protein, over-carb and over-fat more than the opposite
        misses. Built as a closure with everything bound to locals because the
        optimizer calls it hundreds of thousands of times.
        """
        tp, tc, tf = targets["p"], targets["c"], targets["f"]
        wp, wc, wf = weights["p"], weights["c"], weights["f"]
        under_p, over_c, over_f = penalties["under_p"], penalties["over_c"], penalties["over_f"]
        sqrt = math.sqrt

        def score(p, c, f):
            ep, ec, ef = p - tp, c - tc, f - tf
            if ep < 0:
                ep *= under_p
            if ec > 0:
                ec *= over_c
            if ef > 0:
                ef *= over_f
            return sqrt(wp * ep * ep + wc * ec * ec + wf * ef * ef)

        return score

    def _calculate_score(self, meal_plan, targets, weights, penalties):
        """Scores a meal plan; lower is better. Penalises under-protein / over-carb / over-fat."""
        if not meal_plan:
            return float("inf"), {}

        totals = {
            "p": sum(item.get("p", 0) for item in meal_plan),
            "c": sum(item.get("c", 0) for item in meal_plan),
            "f": sum(item.get("f", 0) for item in meal_plan),
        }
        score = self._make_scorer(targets, weights, penalties)(totals["p"], totals["c"], totals["f"])
        return score, totals

    # --- menu loading (date-parameterised) ---

    def _fetch_court_menu(self, court, date_str, cached_data):
        """Fetches one court's menu for a date, using cache if present.

        Returns (court, menu_data, was_fetched, ok). `ok` is False when the
        request itself failed, so the load can be retried later.
        """
        menu_data = cached_data.get(court)
        if menu_data:
            return court, menu_data, False, True

        try:
            variables = {"courtName": court, "date": date_str}
            resp = requests.post(
                self.url,
                json={"query": self.GRAPHQL_QUERY, "variables": variables},
                headers=self.headers,
                timeout=Config.API_TIMEOUT,
            )
            resp.raise_for_status()
            payload = resp.json()
        except requests.exceptions.Timeout:
            logger.error(f"Timeout fetching menu for {court} on {date_str}")
            return court, None, False, False
        except (requests.exceptions.RequestException, ValueError) as e:
            logger.error(f"Error fetching menu for {court} on {date_str}: {e}")
            return court, None, False, False

        if isinstance(payload, dict) and payload.get("errors"):
            logger.error(f"Purdue API returned errors for {court} on {date_str}: {payload['errors']}")
            return court, payload, True, False
        return court, payload, True, True

    @staticmethod
    def _response_has_menu(menu_data):
        """True if a GraphQL response actually carries a published daily menu.

        Purdue returns HTTP 200 with `dailyMenu: null` (or a GraphQL `errors`
        block) for days it hasn't published yet. Caching those to disk would
        pin an empty menu forever, so we only persist real data.
        """
        if not isinstance(menu_data, dict) or "data" not in menu_data:
            return False
        court = (menu_data.get("data") or {}).get("diningCourtByName")
        return bool(court and court.get("dailyMenu"))

    def _cache_is_fresh(self, payload, date_str):
        """Whether an on-disk cache payload may still be used.

        Caches written by an older query (different CACHE_VERSION) are never
        reused. Past dates never go stale. Today-or-later is trusted only for
        Config.CACHE_TTL_HOURS after it was written.
        """
        if not isinstance(payload, dict):
            return False
        if payload.get("version") != Config.CACHE_VERSION:
            return False
        if date_str < self._today():
            return True
        written = self._as_purdue_time(payload.get("timestamp"))
        if written is None:
            return False
        return self._now() - written < timedelta(hours=Config.CACHE_TTL_HOURS)

    def _food_from_appearance(self, appearance, court, meal_name, station):
        """One food dict for an item appearance, or None if it has no usable nutrition."""
        item = appearance.get("item") or {}
        facts = item.get("nutritionFacts") or []
        if not facts:
            return None

        food = {"p": 0.0, "c": 0.0, "f": 0.0, "cal": 0.0}
        serving_size = ""
        for fact in facts:
            name = fact.get("name")
            if name in _MACRO_FACTS:
                food[_MACRO_FACTS[name]] = self._nutrient(fact)
            elif name == "Serving Size":
                serving_size = fact.get("label") or ""

        if food["p"] + food["c"] + food["f"] <= 0:
            return None

        food.update({
            "name": (appearance.get("displayName") or "").strip() or "Unnamed item",
            "court": court,
            "meal_name": meal_name,
            "station": station or "",
            "traits": [t["name"] for t in (item.get("traits") or []) if t and t.get("name")],
            "serving_size": serving_size,
        })
        return food

    def _foods_in(self, appearance, court, meal_name, station, depth=0):
        """Yield every food in an item appearance, expanding composite stations.

        Entries like "Salad Bar", "Deli Bar" or "La Fonda Station" have no
        nutrition of their own; their foods are listed as components. Those are
        offered individually, labelled with the station they're served at.
        """
        food = self._food_from_appearance(appearance, court, meal_name, station)
        if food is not None:
            yield food
            return
        if depth >= 2:
            return
        parent = (appearance.get("displayName") or "").strip() or station
        for component in appearance.get("components") or []:
            yield from self._foods_in(component, court, meal_name, parent, depth + 1)

    def _parse_court(self, court, menu_data, seen):
        """Flatten one court's GraphQL response into (foods, hours).

        `seen` de-duplicates foods by (court, meal, name): Purdue often lists the
        same item at several stations, and a plate should never list it twice.
        """
        foods, hours = [], {}
        daily_menu = menu_data["data"]["diningCourtByName"]["dailyMenu"]
        for meal in daily_menu.get("meals") or []:
            meal_name = meal.get("name")
            if not meal_name:
                continue
            hours[(court, meal_name)] = {
                "start": self._as_purdue_time(meal.get("startTime")),
                "end": self._as_purdue_time(meal.get("endTime")),
                "status": meal.get("status"),
            }
            for station in meal.get("stations") or []:
                for appearance in station.get("items") or []:
                    for food in self._foods_in(appearance, court, meal_name, station.get("name")):
                        key = (court, meal_name, food["name"].lower())
                        if key in seen:
                            continue
                        seen.add(key)
                        foods.append(food)
        return foods, hours

    def _get_load_lock(self, date_str):
        """Returns a per-date lock; creates one on first use."""
        with self._load_locks_meta:
            lock = self._date_load_locks.get(date_str)
            if lock is None:
                lock = threading.Lock()
                self._date_load_locks[date_str] = lock
            return lock

    def _get_snapshot(self, date_str):
        with self.data_lock:
            return self.menu_by_date.get(date_str)

    def has_data(self, date_str):
        with self.data_lock:
            return date_str in self.menu_by_date

    def _snapshot_is_stale(self, snapshot, date_str):
        """Whether an in-memory snapshot should be refreshed.

        Incomplete or empty loads (a court failed, or Purdue hadn't published
        yet) are retried after a few minutes. Today-or-later menus expire after
        the cache TTL, because Purdue keeps editing them. Past dates are final.
        """
        loaded_at = snapshot.get("loaded_at")
        if loaded_at is None:
            return False  # injected snapshot without load metadata (tests)
        age = self._now() - loaded_at
        if not snapshot.get("complete", True) or not snapshot.get("master"):
            return age > timedelta(minutes=Config.INCOMPLETE_RETRY_MINUTES)
        if date_str < self._today():
            return False
        return age > timedelta(hours=Config.CACHE_TTL_HOURS)

    def _load_menu_for_date(self, date_str, refresh=False):
        """Load (or, with refresh=True, re-load a stale) menu for a date.

        Idempotent and safe to call from multiple threads.
        """
        if not refresh and self.has_data(date_str):
            return

        # Per-date lock so concurrent requests for the same date don't double-fetch.
        with self._get_load_lock(date_str):
            existing = self._get_snapshot(date_str)
            if existing is not None and (not refresh or not self._snapshot_is_stale(existing, date_str)):
                return

            cache_file = self._menu_cache_path(date_str)
            cached_data, cache_written_at = {}, None

            if os.path.exists(cache_file):
                try:
                    with open(cache_file, "r") as f:
                        payload = json.load(f)
                    if self._cache_is_fresh(payload, date_str):
                        cached_data = payload.get("data") or {}
                        cache_written_at = self._as_purdue_time(payload.get("timestamp"))
                        logger.info(f"Loaded {cache_file} from disk.")
                    else:
                        logger.info(f"Cache {cache_file} is stale or from an older version; refetching.")
                except (json.JSONDecodeError, IOError, TypeError) as e:
                    logger.error(f"Error loading cache file {cache_file}: {e}")

            logger.info(f"Loading menu data for {date_str}...")
            foods, hours, seen = [], {}, set()
            complete, fetched_any = True, False

            with ThreadPoolExecutor(max_workers=len(self.dining_courts)) as executor:
                futures = [
                    executor.submit(self._fetch_court_menu, court, date_str, cached_data)
                    for court in self.dining_courts
                ]
                for future in futures:
                    court, menu_data, was_fetched, ok = future.result()
                    complete = complete and ok
                    if not self._response_has_menu(menu_data):
                        continue
                    # Only persist responses that actually contain a menu, so an
                    # unpublished future date isn't cached as permanently empty.
                    if was_fetched:
                        cached_data[court] = menu_data
                        fetched_any = True
                    try:
                        court_foods, court_hours = self._parse_court(court, menu_data, seen)
                    except (KeyError, TypeError, AttributeError):
                        logger.exception(f"Malformed menu for {court} on {date_str}")
                        complete = False
                        continue
                    foods.extend(court_foods)
                    hours.update(court_hours)

            # Data reused from disk is only as fresh as the file it came from.
            loaded_at = cache_written_at or self._now()
            with self.data_lock:
                self.menu_by_date[date_str] = {
                    "master": foods,
                    "hours": hours,
                    "loaded_at": loaded_at,
                    "complete": complete,
                }

            if fetched_any:
                self._write_cache(cache_file, cached_data, loaded_at)

            courts = {f["court"] for f in foods}
            logger.info(
                f"Menu for {date_str}: {len(foods)} items from {len(courts)} courts"
                f"{'' if complete else ' (incomplete; will retry)'}."
            )

    def _write_cache(self, cache_file, cached_data, written_at):
        """Atomically persist raw court responses so a reader never sees a half-written file."""
        tmp = f"{cache_file}.tmp"
        try:
            with open(tmp, "w") as f:
                json.dump({
                    "version": Config.CACHE_VERSION,
                    "timestamp": written_at.isoformat(),
                    "data": cached_data,
                }, f)
            os.replace(tmp, cache_file)
            logger.info(f"Saved menu cache to {cache_file}")
        except (IOError, OSError) as e:
            logger.error(f"Error saving cache file {cache_file}: {e}")

    def _start_background_load(self, date_str, refresh=False):
        """Start at most one background load per date."""
        with self._load_locks_meta:
            if date_str in self._loading:
                return
            self._loading.add(date_str)

        def run():
            try:
                self._load_menu_for_date(date_str, refresh=refresh)
            except Exception:
                logger.exception(f"Background menu load failed for {date_str}")
            finally:
                with self._load_locks_meta:
                    self._loading.discard(date_str)

        threading.Thread(target=run, daemon=True, name=f"MenuLoader-{date_str}").start()

    def ensure_loaded(self, date_str):
        """Make sure a menu for `date_str` is in memory.

        The first load blocks. A stale snapshot keeps being served while a
        fresh copy loads in the background.
        """
        snapshot = self._get_snapshot(date_str)
        if snapshot is None:
            self._load_menu_for_date(date_str)
        elif self._snapshot_is_stale(snapshot, date_str):
            self._start_background_load(date_str, refresh=True)

    def ensure_loaded_async(self, date_str):
        """Kick off a load for `date_str` in the background and return at once.

        Lets the API answer a cold request with 503 instead of blocking for the
        full Purdue fetch. Idempotent — a load already in flight is a no-op.
        """
        if not self.has_data(date_str):
            self._start_background_load(date_str)

    def start_background_loaders(self):
        """Preload today's menu, then tomorrow's, so the first "right now"
        request and the common "plan tomorrow" request are both instant.

        One thread, one date at a time, to stay gentle on Purdue's API. A
        request for either date meanwhile waits on the same per-date lock
        instead of fetching it twice.
        """
        today = self._now().date()
        dates = [d.strftime("%Y-%m-%d") for d in (today, today + timedelta(days=1))]
        logger.info(f"Preloading menus for {', '.join(dates)}...")

        def run():
            for date_str in dates:
                try:
                    self._load_menu_for_date(date_str)
                except Exception:
                    logger.exception(f"Preloading the menu for {date_str} failed")

        threading.Thread(target=run, daemon=True, name="MenuPreloader").start()

    # --- meal-finding algorithm ---

    def _optimize_pool(self, items, targets, weights, penalties, rng=None):
        """Pick a plate of MIN_ITEMS..MAX_ITEMS servings whose totals best match targets.

        A plate is a multiset of pool indices: each food may appear up to
        MAX_SERVINGS times, and every serving counts toward the item limits.
        Simulated annealing runs from several greedy seeds, and each result is
        finished with a local search over every 1- and 2-serving change.
        Totals are updated incrementally, so each candidate costs O(1) to score.

        Returns (plan, score, totals), or (None, inf, {}) if the pool is too
        small. Plan entries are copies of pool items with a `servings` count.
        """
        rng = rng or random
        n = len(items)
        if n < Config.MIN_ITEMS:
            return None, float("inf"), {}

        P = [float(it.get("p", 0)) for it in items]
        C = [float(it.get("c", 0)) for it in items]
        F = [float(it.get("f", 0)) for it in items]
        cal = [float(it.get("cal", 0) or 0) for it in items]
        score = self._make_scorer(targets, weights, penalties)

        # The greedy plate is built around its first picks, often one large
        # item (say a 32 g-protein entree). A better plate may need that item
        # gone and several smaller ones added at once, which no single
        # swap/add/remove can do, so also start from greedy plates that are
        # forbidden each early pick. Later passes may still bring it back.
        restarts = max(1, Config.RESTARTS)
        greedy = self._greedy_seed(n, P, C, F, score)
        seeds = [greedy]
        for anchor in list(dict.fromkeys(greedy))[:restarts - 1]:
            seed = self._greedy_seed(n, P, C, F, score, banned={anchor})
            if len(seed) >= Config.MIN_ITEMS:  # tiny pools may run out of items
                seeds.append(seed)
        while len(seeds) < restarts:
            seeds.append(rng.sample(range(n), min(Config.INITIAL_ITEMS, n)))

        protein_window = self._make_protein_window(targets, weights, penalties)
        best_plate, best_score = None, float("inf")
        for plate in seeds:
            plate = self._anneal(plate, n, P, C, F, score, rng)
            plate, plate_score = self._deep_polish(plate, n, P, C, F, score, protein_window)
            if plate_score < best_score:
                best_plate, best_score = plate, plate_score

        servings = {}
        for i in best_plate:
            servings[i] = servings.get(i, 0) + 1
        # Copies, so the shared menu snapshot is never mutated per request.
        plan = [dict(items[i], servings=count) for i, count in servings.items()]
        plan.sort(key=lambda it: -it.get("p", 0) * it["servings"])
        totals = {
            "p": round(sum(P[i] for i in best_plate), 1),
            "c": round(sum(C[i] for i in best_plate), 1),
            "f": round(sum(F[i] for i in best_plate), 1),
            "cal": round(sum(cal[i] for i in best_plate)),
        }
        return plan, best_score, totals

    @staticmethod
    def _greedy_seed(n, P, C, F, score, banned=frozenset()):
        """Add whichever serving improves the score most until nothing helps."""
        cap = Config.MAX_SERVINGS
        plate, counts = [], [0] * n
        tp = tc = tf = 0.0
        current = float("inf")
        while len(plate) < Config.MAX_ITEMS:
            best_j, best_s = None, float("inf")
            for j in range(n):
                if counts[j] >= cap or j in banned:
                    continue
                s = score(tp + P[j], tc + C[j], tf + F[j])
                if s < best_s:
                    best_j, best_s = j, s
            if best_j is None or (len(plate) >= Config.MIN_ITEMS and best_s >= current):
                break
            plate.append(best_j)
            counts[best_j] += 1
            tp, tc, tf = tp + P[best_j], tc + C[best_j], tf + F[best_j]
            current = best_s
        return plate

    @staticmethod
    def _anneal(plate, n, P, C, F, score, rng):
        """Simulated annealing over swap/add/remove moves. Returns the best plate seen."""
        lo, hi, cap = Config.MIN_ITEMS, Config.MAX_ITEMS, Config.MAX_SERVINGS
        plate = list(plate)
        counts = [0] * n
        for i in plate:
            counts[i] += 1
        tp, tc, tf = sum(P[i] for i in plate), sum(C[i] for i in plate), sum(F[i] for i in plate)
        current = score(tp, tc, tf)
        best_plate, best = list(plate), current

        iterations = Config.ITERATIONS
        if iterations <= 0:
            return best_plate
        # Start hot relative to *this* problem's scores, cool to FINAL_TEMP.
        temp = max(current, 1.0)
        alpha = (Config.FINAL_TEMP / temp) ** (1.0 / iterations)
        exp, rand, randrange = math.exp, rng.random, rng.randrange

        for _ in range(iterations):
            move = rand()
            if move < 0.6:  # swap one serving for a different food
                k, j = randrange(len(plate)), randrange(n)
                i = plate[k]
                if j != i and counts[j] < cap:
                    np_, nc, nf = tp - P[i] + P[j], tc - C[i] + C[j], tf - F[i] + F[j]
                    s = score(np_, nc, nf)
                    if s <= current or rand() < exp((current - s) / temp):
                        plate[k] = j
                        counts[i] -= 1
                        counts[j] += 1
                        tp, tc, tf, current = np_, nc, nf, s
            elif move < 0.8:  # add a serving
                if len(plate) < hi:
                    j = randrange(n)
                    if counts[j] < cap:
                        np_, nc, nf = tp + P[j], tc + C[j], tf + F[j]
                        s = score(np_, nc, nf)
                        if s <= current or rand() < exp((current - s) / temp):
                            plate.append(j)
                            counts[j] += 1
                            tp, tc, tf, current = np_, nc, nf, s
            elif len(plate) > lo:  # remove a serving
                k = randrange(len(plate))
                i = plate[k]
                np_, nc, nf = tp - P[i], tc - C[i], tf - F[i]
                s = score(np_, nc, nf)
                if s <= current or rand() < exp((current - s) / temp):
                    plate.pop(k)
                    counts[i] -= 1
                    tp, tc, tf, current = np_, nc, nf, s

            if current < best:
                best, best_plate = current, list(plate)
            temp *= alpha

        return best_plate

    @staticmethod
    def _polish(plate, n, P, C, F, score):
        """Best-improvement local search until no single-serving change helps.

        Guarantees the returned plate is a local optimum, which annealing alone
        does not.
        """
        lo, hi, cap = Config.MIN_ITEMS, Config.MAX_ITEMS, Config.MAX_SERVINGS
        plate = list(plate)
        counts = [0] * n
        for i in plate:
            counts[i] += 1
        tp, tc, tf = sum(P[i] for i in plate), sum(C[i] for i in plate), sum(F[i] for i in plate)
        current = score(tp, tc, tf)

        while True:
            best_move, best_s = None, current - 1e-9
            addable = [j for j in range(n) if counts[j] < cap]
            distinct = list(dict.fromkeys(plate))  # two servings of a food are interchangeable
            if len(plate) > lo:
                for i in distinct:
                    s = score(tp - P[i], tc - C[i], tf - F[i])
                    if s < best_s:
                        best_move, best_s = ("remove", i, None), s
            if len(plate) < hi:
                for j in addable:
                    s = score(tp + P[j], tc + C[j], tf + F[j])
                    if s < best_s:
                        best_move, best_s = ("add", None, j), s
            for i in distinct:
                bp, bc, bf = tp - P[i], tc - C[i], tf - F[i]
                for j in addable:
                    if j == i:
                        continue
                    s = score(bp + P[j], bc + C[j], bf + F[j])
                    if s < best_s:
                        best_move, best_s = ("swap", i, j), s
            if best_move is None:
                break

            kind, i, j = best_move
            if kind in ("remove", "swap"):
                plate.remove(i)  # one serving of food i
                counts[i] -= 1
                tp, tc, tf = tp - P[i], tc - C[i], tf - F[i]
            if kind in ("add", "swap"):
                plate.append(j)
                counts[j] += 1
                tp, tc, tf = tp + P[j], tc + C[j], tf + F[j]
            current = best_s

        return plate, current

    @staticmethod
    def _make_protein_window(targets, weights, penalties):
        """Returns window(residual_protein, best_score) -> (lo, hi).

        Any pair of servings whose combined protein falls outside [lo, hi]
        cannot beat best_score, because the protein term alone would already
        exceed it. This lets the two-serving search skip almost every pair exactly.
        """
        tp = targets["p"]
        root_w = math.sqrt(weights["p"]) if weights["p"] > 0 else 0.0
        under = penalties["under_p"]

        def window(residual_p, best):
            if root_w == 0 or under <= 0 or best == float("inf"):
                return float("-inf"), float("inf")
            needed = tp - residual_p
            return needed - best / (root_w * under), needed + best / root_w

        return window

    @staticmethod
    def _deep_polish(plate, n, P, C, F, score, protein_window):
        """Local search that may change up to two servings at once.

        Single swap/add/remove moves get stuck when the plate is built around
        one dominant item: replacing it needs it removed and two or three
        smaller items added together. After single moves converge, this tries
        every combination of removing 0-2 servings and adding 1-2 servings
        (including two servings of one food), applies the best improvement,
        and repeats until nothing helps.
        """
        lo_items, hi_items, cap = Config.MIN_ITEMS, Config.MAX_ITEMS, Config.MAX_SERVINGS
        order = sorted(range(n), key=P.__getitem__)
        sorted_p = [P[i] for i in order]
        plate, current = MealFinder._polish(plate, n, P, C, F, score)

        while True:
            counts = [0] * n
            for i in plate:
                counts[i] += 1
            tp, tc, tf = sum(P[i] for i in plate), sum(C[i] for i in plate), sum(F[i] for i in plate)
            size_now = len(plate)
            best_s, best_move = current - 1e-9, None

            # Removals as multisets of foods: nothing, one serving, or two
            # servings (of two foods, or both servings of one food).
            distinct = list(dict.fromkeys(plate))
            removals = [()] + [(i,) for i in distinct] + [
                (a, b) for x, a in enumerate(distinct) for b in distinct[x:]
                if a != b or counts[a] >= 2
            ]
            for rem in removals:
                for i in rem:
                    counts[i] -= 1
                rp = tp - sum(P[i] for i in rem)
                rc = tc - sum(C[i] for i in rem)
                rf = tf - sum(F[i] for i in rem)
                size = size_now - len(rem)

                # Add two servings (covers add-two, split one into two, and
                # double swap), scanning only pairs inside the protein window.
                if lo_items <= size + 2 <= hi_items:
                    w_lo, w_hi = protein_window(rp, best_s)
                    for ia in range(n):
                        a = order[ia]
                        room_a = cap - counts[a]
                        if room_a < 1:
                            continue
                        pa = sorted_p[ia]
                        # Starting at ia itself also tries two servings of `a`.
                        start = bisect.bisect_left(sorted_p, w_lo - pa, ia)
                        end = bisect.bisect_right(sorted_p, w_hi - pa, ia)
                        ca, fa = C[a], F[a]
                        for ib in range(start, end):
                            b = order[ib]
                            if b == a:
                                if room_a < 2:
                                    continue
                            elif counts[b] >= cap:
                                continue
                            s = score(rp + pa + sorted_p[ib], rc + ca + C[b], rf + fa + F[b])
                            if s < best_s:
                                best_s, best_move = s, (rem, (a, b))

                if len(rem) == 2:
                    # Merge two servings into one.
                    if lo_items <= size + 1 <= hi_items:
                        for j in range(n):
                            if counts[j] < cap:
                                s = score(rp + P[j], rc + C[j], rf + F[j])
                                if s < best_s:
                                    best_s, best_move = s, (rem, (j,))
                    # Drop two servings.
                    if size >= lo_items:
                        s = score(rp, rc, rf)
                        if s < best_s:
                            best_s, best_move = s, (rem, ())

                for i in rem:
                    counts[i] += 1

            if best_move is None:
                return plate, current

            rem, add = best_move
            plate = list(plate)
            for i in rem:
                plate.remove(i)
            plate.extend(add)
            plate, current = MealFinder._polish(plate, n, P, C, F, score)

    @staticmethod
    def _passes_filters(item, dietary_filters):
        traits = set(item.get("traits") or [])
        if dietary_filters.get("Vegan") and "Vegan" not in traits:
            return False
        if dietary_filters.get("Vegetarian") and not traits & {"Vegetarian", "Vegan"}:
            return False
        if dietary_filters.get("No Gluten") and traits & _GLUTEN_TRAITS:
            return False
        return True

    def _hours_for(self, snapshot, court, meal_name):
        h = (snapshot.get("hours") or {}).get((court, meal_name)) or {}
        return _hours_payload(h.get("start"), h.get("end"))

    @staticmethod
    def _coerce_targets(targets):
        return {k: float(targets[k]) for k in ("p", "c", "f")}

    def _build_pools(self, items, include, dietary_filters, exclusion_list):
        """Group eligible foods into (court, meal) pools where include(court, meal) holds."""
        # Forgiving, case-insensitive substring match so users can type
        # "eggs" and exclude "Scrambled Eggs" without knowing the exact name.
        excluded_terms = [
            str(term).strip().lower() for term in (exclusion_list or []) if str(term).strip()
        ]
        pools, seen = {}, set()
        for item in items:
            key = (item["court"], item["meal_name"])
            if not include(*key):
                continue
            if item.get("p", 0) + item.get("c", 0) + item.get("f", 0) < Config.MIN_ITEM_MACROS:
                continue
            if not self._passes_filters(item, dietary_filters):
                continue
            name = (item.get("name") or "").lower()
            if any(term in name for term in excluded_terms) or (key, name) in seen:
                continue
            seen.add((key, name))
            pools.setdefault(key, []).append(item)
        return pools

    def _plan_from_pools(self, snapshot, pools, targets, date_str):
        """Optimize every pool. Returns the best plate, plus an `options` list
        holding the best plate at each dining court (best first), or None.

        Each (court, meal period) is optimized separately: a plate can only use
        food served at one place at one time, so lunch and dinner items are
        never combined.
        """
        best_by_court = {}
        for (court, meal_name), pool in pools.items():
            plan, score, totals = self._optimize_pool(
                pool, targets, Config.WEIGHTS, Config.PENALTIES
            )
            if not plan:
                continue
            if court not in best_by_court or score < best_by_court[court][0]:
                best_by_court[court] = (score, {
                    "score": round(score, 3),
                    "court": court,
                    "meal_name": meal_name,
                    "hours": self._hours_for(snapshot, court, meal_name),
                    "plan": plan,
                    "totals": totals,
                })
        if not best_by_court:
            return None

        options = [option for _, option in sorted(best_by_court.values(), key=lambda pair: pair[0])]
        loaded_at = snapshot.get("loaded_at")
        return {
            **options[0],
            "options": options,
            "targets": targets,
            "date": date_str,
            "menu_updated": loaded_at.isoformat() if loaded_at else None,
        }

    def find_best_meal(
        self,
        targets,
        meal_periods_to_check,
        exclusion_list=None,
        dietary_filters=None,
        date_str=None,
    ):
        """Best plate for the date and meal periods across all dining courts,
        plus each court's best plate in `options`. None if nothing fits."""
        targets = self._coerce_targets(targets)
        date_str = self.normalize_date(date_str)
        self.ensure_loaded(date_str)

        snapshot = self._get_snapshot(date_str)
        if not snapshot or not snapshot.get("master"):
            logger.warning(f"Menu for {date_str} is empty (Purdue may not have published it).")
            return None

        wanted = set(meal_periods_to_check or [])
        pools = self._build_pools(
            snapshot["master"], lambda court, meal: meal in wanted,
            dietary_filters or {}, exclusion_list,
        )
        if not pools:
            logger.warning(
                f"No food fits date={date_str} meals={sorted(wanted)} filters={dietary_filters}"
            )
            return None
        return self._plan_from_pools(snapshot, pools, targets, date_str)

    def find_meal_now(self, targets, exclusion_list=None, dietary_filters=None, now=None):
        """Best plate from the meals being served right now, by Purdue's
        published per-court hours.

        Returns {"currently_serving": False, "next_meal_hint": ...} when nothing
        is open, None when meals are open but no food passes the filters, and
        otherwise the same payload as find_best_meal with currently_serving=True.
        """
        targets = self._coerce_targets(targets)
        now = self._as_purdue_time(now) if now is not None else self._now()
        date_str = now.strftime("%Y-%m-%d")
        self.ensure_loaded(date_str)

        snapshot = self._get_snapshot(date_str) or {}
        items = snapshot.get("master") or []
        if not items:
            return {
                "currently_serving": False,
                "next_meal_hint": "Purdue hasn't published today's menu yet",
                "date": date_str,
            }

        open_pairs, hint = self._open_meals(items, snapshot.get("hours") or {}, now)
        if not open_pairs:
            return {"currently_serving": False, "next_meal_hint": hint, "date": date_str}

        pools = self._build_pools(
            items, lambda court, meal: (court, meal) in open_pairs,
            dietary_filters or {}, exclusion_list,
        )
        result = self._plan_from_pools(snapshot, pools, targets, date_str) if pools else None
        if result is not None:
            result["currently_serving"] = True
        return result

    # --- what's open when ---

    @staticmethod
    def _meal_periods_at(now):
        """Fallback schedule: (currently_serving, next_label) from typical hours.

        Used only when Purdue's API doesn't publish meal times. currently_serving
        is a list of meal-period names; next_label is a hint like "Lunch starts
        at 11 am" when nothing is open.
        """
        h = now.hour + now.minute / 60
        is_weekend = now.weekday() >= 5
        windows = [
            (name, start, end)
            for name, start, end, weekend_only in _FALLBACK_HOURS
            if is_weekend or not weekend_only
        ]

        currently = [name for name, start, end in windows if start <= h < end]
        if currently:
            return currently, None

        upcoming = sorted((start, name) for name, start, _ in windows if start > h)
        if upcoming:
            start, name = upcoming[0]
            return [], f"{name} starts at {_format_clock(start)}"
        return [], "Breakfast starts at 7 am tomorrow"

    def _open_meals(self, items, hours, at):
        """Which (court, meal) pairs are serving at `at`, plus a hint if none are.

        Uses Purdue's published per-court hours when available, falling back to
        the typical schedule by meal name otherwise.
        """
        pairs = {(it["court"], it["meal_name"]) for it in items}
        timed = {
            key: h for key, h in hours.items()
            if key in pairs and h.get("start") and h.get("end")
        }
        if timed:
            open_now = {
                key for key, h in timed.items()
                if h["start"] <= at < h["end"] and h.get("status") != "CLOSED"
            }
            if open_now:
                return open_now, None
            upcoming = sorted(
                (h["start"], key[1]) for key, h in timed.items()
                if h["start"] > at and h.get("status") != "CLOSED"
            )
            if upcoming:
                start, meal = upcoming[0]
                return set(), f"{meal} starts at {_format_clock(start.hour, start.minute)}"
            return set(), "Dining courts are closed until tomorrow"

        names, hint = self._meal_periods_at(at)
        return {p for p in pairs if p[1] in names}, hint
