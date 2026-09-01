import threading
import requests
import json
import re
import random
import math
import os
import logging
from datetime import datetime, date as date_cls, timedelta
from concurrent.futures import ThreadPoolExecutor
from config import Config

logger = logging.getLogger(__name__)


class MealFinder:
    """Backend for fetching Purdue dining-court data and finding optimal meal plans.

    Data is keyed by date (YYYY-MM-DD). Each date has its own in-memory snapshot
    and disk cache file, loaded lazily on first request.
    """

    GRAPHQL_QUERY = """
    query GetMenu($courtName: String!, $date: Date!) {
      diningCourtByName(name: $courtName) {
        name
        dailyMenu(date: $date) {
          meals { name, stations { name, items { displayName, item { traits { name }, nutritionFacts { name, label } } } } }
        }
      }
    }
    """

    def __init__(self):
        self.url = Config.PURDUE_API_URL
        self.headers = {"Content-Type": "application/json"}
        self.dining_courts = Config.DINING_COURTS

        # Per-date menu snapshots: date_str -> {master, by_court, by_meal}
        self.menu_by_date = {}

        # One lock for all menu data. Coarse but adequate.
        self.data_lock = threading.Lock()

        # Per-date load locks so two threads asking for the same date don't both
        # hit the Purdue API. Created on demand.
        self._date_load_locks = {}
        self._load_locks_meta = threading.Lock()

    # --- date helpers ---

    @staticmethod
    def _today():
        return datetime.now().strftime("%Y-%m-%d")

    @staticmethod
    def normalize_date(date_str):
        """Returns a valid YYYY-MM-DD string, defaulting to today.

        Raises ValueError if the input is malformed or unreasonable.
        """
        if not date_str:
            return MealFinder._today()
        try:
            parsed = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError as exc:
            raise ValueError(f"Date must be YYYY-MM-DD, got {date_str!r}") from exc
        today = date_cls.today()
        # Allow up to a year back and 30 days forward — anything outside that
        # window is almost certainly a mistake or stale state from the client.
        if parsed < today - timedelta(days=365) or parsed > today + timedelta(days=30):
            raise ValueError(
                f"Date {parsed} is outside the supported window "
                f"(1 year back to 30 days forward)."
            )
        return parsed.strftime("%Y-%m-%d")

    def _menu_cache_path(self, date_str):
        return f"{Config.CACHE_PREFIX_MENU}{date_str}.json"

    # --- numeric / scoring (unchanged from the original implementation) ---

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

    def _calculate_score(self, meal_plan, targets, weights, penalties):
        """Scores a meal plan; lower is better. Penalises under-protein / over-carb / over-fat."""
        if not meal_plan:
            return float("inf"), {}

        totals = {
            "p": sum(item.get("p", 0) for item in meal_plan),
            "c": sum(item.get("c", 0) for item in meal_plan),
            "f": sum(item.get("f", 0) for item in meal_plan),
        }
        errors = {
            "p": totals["p"] - targets["p"],
            "c": totals["c"] - targets["c"],
            "f": totals["f"] - targets["f"],
        }
        if errors["p"] < 0:
            errors["p"] *= penalties["under_p"]
        if errors["c"] > 0:
            errors["c"] *= penalties["over_c"]
        if errors["f"] > 0:
            errors["f"] *= penalties["over_f"]

        score = (
            weights["p"] * (errors["p"] ** 2)
            + weights["c"] * (errors["c"] ** 2)
            + weights["f"] * (errors["f"] ** 2)
        ) ** 0.5
        return score, totals

    # --- menu loading (now date-parameterised) ---

    def _fetch_court_menu(self, court, date_str, cached_data):
        """Fetches menu data for a single court on a given date, using cache if present."""
        menu_data = cached_data.get(court)
        if menu_data:
            return court, menu_data, False

        try:
            variables = {"courtName": court, "date": date_str}
            resp = requests.post(
                self.url,
                json={"query": self.GRAPHQL_QUERY, "variables": variables},
                headers=self.headers,
                timeout=Config.API_TIMEOUT,
            )
            resp.raise_for_status()
            return court, resp.json(), True
        except requests.exceptions.Timeout:
            logger.error(f"Timeout fetching menu for {court} on {date_str}")
            return court, None, False
        except requests.exceptions.RequestException as e:
            logger.error(f"Error fetching menu for {court} on {date_str}: {e}")
            return court, None, False

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

        Past dates never go stale. Today-or-later is trusted only for
        Config.CACHE_TTL_HOURS after it was written.
        """
        if not isinstance(payload, dict):
            return False
        if date_str < self._today():
            return True
        ts = payload.get("timestamp")
        if not ts:
            return False
        try:
            written = datetime.fromisoformat(ts)
        except ValueError:
            return False
        return datetime.now() - written < timedelta(hours=Config.CACHE_TTL_HOURS)

    @staticmethod
    def _build_indices(item_list):
        by_court, by_meal = {}, {}
        for item in item_list:
            by_court.setdefault(item["court"], []).append(item)
            by_meal.setdefault(item["meal_name"], []).append(item)
        return by_court, by_meal

    def _get_load_lock(self, date_str):
        """Returns a per-date lock; creates one on first use."""
        with self._load_locks_meta:
            lock = self._date_load_locks.get(date_str)
            if lock is None:
                lock = threading.Lock()
                self._date_load_locks[date_str] = lock
            return lock

    def has_data(self, date_str):
        with self.data_lock:
            return date_str in self.menu_by_date

    def _load_menu_for_date(self, date_str):
        """Idempotent menu load for a specific date. Safe to call from multiple threads."""
        if self.has_data(date_str):
            return

        # Per-date lock so concurrent requests for the same date don't double-fetch.
        with self._get_load_lock(date_str):
            if self.has_data(date_str):
                return

            cache_file = self._menu_cache_path(date_str)
            cached_data, needs_to_save_cache = {}, False

            if os.path.exists(cache_file):
                try:
                    with open(cache_file, "r") as f:
                        payload = json.load(f)
                    if self._cache_is_fresh(payload, date_str):
                        cached_data = payload.get("data", {})
                        logger.info(f"Loaded {cache_file} from disk.")
                    else:
                        logger.info(f"Cache {cache_file} is stale; refetching.")
                except (json.JSONDecodeError, IOError, TypeError) as e:
                    logger.error(f"Error loading cache file {cache_file}: {e}")

            logger.info(f"Loading menu data for {date_str}...")
            temp_master_list = []

            with ThreadPoolExecutor() as executor:
                futures = {
                    executor.submit(self._fetch_court_menu, court, date_str, cached_data): court
                    for court in self.dining_courts
                }

                for future in futures:
                    court, menu_data, was_fetched = future.result()
                    # Only persist responses that actually contain a menu, so an
                    # unpublished future date isn't cached as permanently empty.
                    if was_fetched and self._response_has_menu(menu_data):
                        cached_data[court] = menu_data
                        needs_to_save_cache = True

                    if not self._response_has_menu(menu_data):
                        continue
                    dining_court = menu_data["data"]["diningCourtByName"]

                    for meal in dining_court["dailyMenu"]["meals"]:
                        for station in meal["stations"]:
                            for item_appearance in station["items"]:
                                core_item = item_appearance.get("item")
                                if not (core_item and core_item.get("nutritionFacts")):
                                    continue

                                macros = {"Protein": 0, "Total Carbohydrate": 0, "Total fat": 0}
                                serving_size = ""
                                for fact in core_item["nutritionFacts"]:
                                    if fact["name"] in macros:
                                        macros[fact["name"]] = self._get_numeric_value(
                                            fact.get("label")
                                        )
                                    elif fact["name"] == "Serving Size":
                                        serving_size = fact.get("label", "")

                                if sum(macros.values()) <= 0:
                                    continue

                                traits = [
                                    t["name"] for t in (core_item.get("traits") or []) if t
                                ]
                                temp_master_list.append({
                                    "name": item_appearance["displayName"],
                                    "p": macros["Protein"],
                                    "c": macros["Total Carbohydrate"],
                                    "f": macros["Total fat"],
                                    "court": court,
                                    "meal_name": meal["name"],
                                    "traits": traits,
                                    "serving_size": serving_size,
                                })

            by_court, by_meal = self._build_indices(temp_master_list)
            with self.data_lock:
                self.menu_by_date[date_str] = {
                    "master": temp_master_list,
                    "by_court": by_court,
                    "by_meal": by_meal,
                }

            if needs_to_save_cache:
                try:
                    with open(cache_file, "w") as f:
                        json.dump(
                            {"timestamp": datetime.now().isoformat(), "data": cached_data}, f
                        )
                    logger.info(f"Saved menu cache to {cache_file}")
                except IOError as e:
                    logger.error(f"Error saving cache file {cache_file}: {e}")

            logger.info(
                f"Menu for {date_str}: {len(temp_master_list)} items, "
                f"{len(by_court)} courts, {len(by_meal)} meals."
            )

    def ensure_loaded(self, date_str):
        """Public entry point — loads the given date if not already loaded."""
        self._load_menu_for_date(date_str)

    def ensure_loaded_async(self, date_str):
        """Kick off a load for `date_str` in the background and return at once.

        Lets the API answer a cold request with 503 instead of blocking for the
        full Purdue fetch. Idempotent — a load already in flight is a no-op.
        """
        if self.has_data(date_str):
            return
        threading.Thread(
            target=self._load_menu_for_date,
            args=(date_str,),
            daemon=True,
            name=f"MenuLoader-{date_str}",
        ).start()

    def start_background_loaders(self):
        """Preloads today's menu data so the first user request is instant."""
        today = self._today()
        logger.info("Starting background loaders for today...")
        threading.Thread(
            target=self._load_menu_for_date,
            args=(today,),
            daemon=True,
            name="MenuLoader",
        ).start()

    # --- meal-finding algorithm ---

    def _run_optimization_for_court(self, available_items, targets, weights, penalties):
        """Simulated annealing within a single court's items."""
        if len(available_items) < Config.MIN_ITEMS:
            return None, float("inf"), {}

        best_solution, best_score, best_totals = None, float("inf"), {}
        temp = Config.INITIAL_TEMP
        cooling_rate = Config.COOLING_RATE
        iterations = Config.ITERATIONS

        initial_size = min(Config.INITIAL_ITEMS, len(available_items))

        # Heuristic seed: start with the two highest-protein items, then random fill.
        sorted_by_protein = sorted(available_items, key=lambda x: x.get("p", 0), reverse=True)
        current_solution = sorted_by_protein[:2]
        items_needed = initial_size - len(current_solution)
        if items_needed > 0:
            remaining_pool = [i for i in available_items if i not in current_solution]
            if remaining_pool:
                current_solution += random.sample(
                    remaining_pool, min(items_needed, len(remaining_pool))
                )

        current_score, _ = self._calculate_score(
            current_solution, targets, weights, penalties
        )

        for _ in range(iterations):
            if temp <= 1:
                break

            neighbor = list(current_solution)
            action = random.choice(["swap", "add", "remove"])

            if action == "swap" and len(neighbor) > 1:
                neighbor[random.randrange(len(neighbor))] = random.choice(available_items)
            elif action == "add" and len(neighbor) < Config.MAX_ITEMS:
                possible_adds = [i for i in available_items if i not in neighbor]
                if possible_adds:
                    neighbor.append(random.choice(possible_adds))
            elif action == "remove" and len(neighbor) > Config.MIN_ITEMS:
                neighbor.pop(random.randrange(len(neighbor)))

            neighbor_score, neighbor_totals = self._calculate_score(
                neighbor, targets, weights, penalties
            )

            if neighbor_score < current_score or random.random() < math.exp(
                (current_score - neighbor_score) / temp
            ):
                current_solution = neighbor
                current_score = neighbor_score

            if neighbor_score < best_score:
                best_score = neighbor_score
                best_totals = neighbor_totals
                best_solution = list(neighbor)

            temp *= cooling_rate

        return best_solution, best_score, best_totals

    def find_best_meal(
        self,
        targets,
        meal_periods_to_check,
        exclusion_list=None,
        dietary_filters=None,
        date_str=None,
    ):
        """Finds the best meal plan for the given date across all dining courts."""
        if exclusion_list is None:
            exclusion_list = []
        if dietary_filters is None:
            dietary_filters = {}

        date_str = self.normalize_date(date_str)
        self.ensure_loaded(date_str)

        with self.data_lock:
            data = self.menu_by_date.get(date_str)
            if not data:
                logger.warning(f"No menu data for {date_str}")
                return None
            snapshot_items = list(data["master"])

        if not snapshot_items:
            logger.warning(f"Menu for {date_str} is empty (Purdue may not have published it).")
            return None

        # Apply dietary filters
        filtered = []
        for item in snapshot_items:
            traits = item.get("traits", [])
            if dietary_filters.get("Vegetarian") and "Vegetarian" not in traits:
                continue
            if dietary_filters.get("Vegan") and "Vegan" not in traits:
                continue
            if dietary_filters.get("No Gluten") and "Contains Gluten" in traits:
                continue
            filtered.append(item)

        available_courts = {
            item["court"]
            for item in filtered
            if item["name"] not in exclusion_list and item["meal_name"] in meal_periods_to_check
        }

        if not available_courts:
            logger.warning(
                f"No courts available for date={date_str} meals={meal_periods_to_check} "
                f"filters={dietary_filters}"
            )
            return None

        overall_best_solution, overall_best_score = None, float("inf")
        weights, penalties = Config.WEIGHTS, Config.PENALTIES

        for court in available_courts:
            court_items = [
                item
                for item in filtered
                if item["court"] == court
                and item["name"] not in exclusion_list
                and item["meal_name"] in meal_periods_to_check
            ]
            solution, score, totals = self._run_optimization_for_court(
                court_items, targets, weights, penalties
            )
            if solution and score < overall_best_score:
                overall_best_score = score
                overall_best_solution = {
                    "score": score,
                    "court": court,
                    "meal_name": solution[0]["meal_name"],
                    "plan": solution,
                    "totals": totals,
                    "date": date_str,
                }

        return overall_best_solution

    # --- featured plate ("right now") ---

    # Approximate dining-court hours (Purdue's API doesn't expose this; these
    # match what's printed on the dining-court signage). The list of meal-period
    # tuples per hour band. Brunch overlaps with breakfast/lunch on weekends.
    @staticmethod
    def _meal_periods_at(now):
        """Returns (currently_serving: List[str], next_label: Optional[str]).

        currently_serving is a list of meal-period names that are open right now.
        next_label is "Lunch starts at 11 am" style hint when nothing is open.
        """
        h = now.hour + now.minute / 60
        is_weekend = now.weekday() >= 5

        currently = []
        if 7 <= h < 11:
            currently.append("Breakfast")
        if is_weekend and 9 <= h < 14:
            currently.append("Brunch")
        if 11 <= h < 14:
            currently.append("Lunch")
        if 14 <= h < 17:
            currently.append("Late Lunch")
        if 17 <= h < 21:
            currently.append("Dinner")

        if currently:
            return currently, None

        # Meal windows run back-to-back from 7 am to 9 pm, so the only gaps are
        # before breakfast and after dinner.
        if h < 7:
            return [], "Breakfast starts at 7 am"
        return [], "Breakfast starts at 7 am tomorrow"

    def _build_featured_plate(self, items, court, meal_name):
        """Picks 3 complementary items from a single court+meal pool: a high-protein
        main, a complementary carb, and a low-fat side. Returns None if we can't
        assemble a sensible plate."""
        if len(items) < 2:
            return None

        # Deduplicate by name (Purdue often lists the same item at multiple stations)
        seen, deduped = set(), []
        for it in items:
            if it["name"] not in seen:
                seen.add(it["name"])
                deduped.append(it)
        items = deduped

        if not items:
            return None

        # 1. Main: highest protein
        sorted_by_p = sorted(items, key=lambda i: i.get("p", 0), reverse=True)
        main = sorted_by_p[0]
        plate = [main]

        # 2. Carb: highest carbs that's not the main and adds some carbs
        carb_candidates = [
            i for i in items
            if i["name"] != main["name"] and i.get("c", 0) >= 10
        ]
        if carb_candidates:
            carb = sorted(carb_candidates, key=lambda i: i.get("c", 0), reverse=True)[0]
            plate.append(carb)

        # 3. Side/veggie: prefer real food, low-fat, with actual substance.
        # Filter out condiments — Purdue's API marks them with serving sizes
        # like "Tablespoon" or "Teaspoon", which is a clean tell.
        used = {p["name"] for p in plate}
        condiment_units = ("tablespoon", "teaspoon", "tbsp", "tsp", "packet", "pkt")
        # Anything whose *name* contains one of these is an auxiliary, not a side dish.
        condiment_words = ("sauce", "dressing", "syrup", "spread", "topping",
                           "dip", "marinade", "glaze", "vinaigrette", "salsa",
                           "ketchup", "mustard", "mayo", "butter")

        def _is_condiment(it):
            ss = (it.get("serving_size") or "").lower()
            name = (it.get("name") or "").lower()
            if any(u in ss for u in condiment_units):
                return True
            if any(w in name for w in condiment_words):
                return True
            return False

        def _is_substantial(it):
            return (it.get("p", 0) + it.get("c", 0) + it.get("f", 0)) >= 8

        substantial_sides = [
            i for i in items
            if i["name"] not in used
            and i.get("f", 0) <= 8
            and _is_substantial(i)
            and not _is_condiment(i)
        ]
        # Bias toward items tagged Vegetarian/Vegan
        substantial_sides.sort(
            key=lambda i: (
                ("Vegan" in i.get("traits", []) or "Vegetarian" in i.get("traits", [])),
                -i.get("f", 0),
            ),
            reverse=True,
        )
        if substantial_sides:
            plate.append(substantial_sides[0])
        # If no substantial side exists, ship the 2-item plate rather than
        # padding with a condiment.

        if len(plate) < 2:
            return None

        totals = {
            "p": sum(i.get("p", 0) for i in plate),
            "c": sum(i.get("c", 0) for i in plate),
            "f": sum(i.get("f", 0) for i in plate),
        }

        return {
            "court": court,
            "meal_name": meal_name,
            "plan": plate,
            "totals": totals,
        }

    def featured_plate(self, date_str=None, now=None):
        """Returns the featured plate for the current moment, or info about the next
        meal if nothing is currently being served.

        Shape:
          {
            currently_serving: bool,
            meal_periods: [...],          # what's open right now
            next_meal_hint: "Lunch starts at 11 am",  # only when not currently_serving
            plate: { court, meal_name, plan, totals },  # only when currently_serving
            date: "YYYY-MM-DD"
          }
        """
        date_str = self.normalize_date(date_str)
        self.ensure_loaded(date_str)
        now = now or datetime.now()

        currently, next_hint = self._meal_periods_at(now)

        # If the requested date is not "today" we still show the result, but
        # the time-of-day check is meaningless for a different date.
        is_today = date_str == self._today()
        if not is_today:
            # For non-today dates, just feature any meal that has items and pick
            # the one most likely to be relevant by hour-of-day.
            currently = currently or ["Lunch", "Dinner", "Late Lunch", "Brunch", "Breakfast"]
            next_hint = None

        if not currently:
            return {
                "currently_serving": False,
                "meal_periods": [],
                "next_meal_hint": next_hint or "No meal currently being served.",
                "plate": None,
                "date": date_str,
            }

        with self.data_lock:
            data = self.menu_by_date.get(date_str)
            items = list(data["master"]) if data else []

        if not items:
            return {
                "currently_serving": False,
                "meal_periods": currently,
                "next_meal_hint": "Menu data is empty for this date.",
                "plate": None,
                "date": date_str,
            }

        # Group available items by (court, meal) and pick the combination with
        # the most items — gives the algorithm room to build a balanced plate.
        groups = {}
        for item in items:
            if item["meal_name"] not in currently:
                continue
            groups.setdefault((item["court"], item["meal_name"]), []).append(item)

        if not groups:
            return {
                "currently_serving": True,
                "meal_periods": currently,
                "next_meal_hint": None,
                "plate": None,
                "date": date_str,
            }

        # Sort by item count (descending) and try to build a plate from each in
        # turn — if one court has too few usable items, fall through to the next.
        for (court, meal_name), pool in sorted(
            groups.items(), key=lambda kv: len(kv[1]), reverse=True
        ):
            plate = self._build_featured_plate(pool, court, meal_name)
            if plate:
                return {
                    "currently_serving": True,
                    "meal_periods": currently,
                    "next_meal_hint": None,
                    "plate": plate,
                    "date": date_str,
                }

        return {
            "currently_serving": True,
            "meal_periods": currently,
            "next_meal_hint": None,
            "plate": None,
            "date": date_str,
        }
