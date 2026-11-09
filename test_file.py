"""
Unit tests for the Purdue Macro Finder
Run with: python -m pytest test_file.py
"""

import itertools
import random
import unittest
from datetime import datetime, time, timedelta, timezone
from meal_finder_engine import MealFinder, PURDUE_TZ
from config import Config


class TestMealFinder(unittest.TestCase):
    """Test cases for MealFinder class"""
    
    def setUp(self):
        """Set up test fixtures"""
        self.finder = MealFinder()
        
    def test_get_numeric_value_with_grams(self):
        """Test extracting numeric value from gram labels"""
        self.assertEqual(self.finder._get_numeric_value("15g"), 15.0)
        self.assertEqual(self.finder._get_numeric_value("20.5g"), 20.5)
        
    def test_get_numeric_value_with_empty_string(self):
        """Test extracting numeric value from empty string"""
        self.assertEqual(self.finder._get_numeric_value(""), 0.0)
        self.assertEqual(self.finder._get_numeric_value(None), 0.0)
        
    def test_get_numeric_value_with_no_number(self):
        """Test extracting numeric value from string with no number"""
        self.assertEqual(self.finder._get_numeric_value("N/A"), 0.0)
        
    def test_calculate_score_with_exact_match(self):
        """Test score calculation when meal plan exactly matches targets"""
        meal_plan = [
            {'p': 25, 'c': 45, 'f': 13}
        ]
        targets = {'p': 25, 'c': 45, 'f': 13}
        weights = Config.WEIGHTS
        penalties = Config.PENALTIES
        
        score, totals = self.finder._calculate_score(meal_plan, targets, weights, penalties)
        
        self.assertEqual(totals['p'], 25)
        self.assertEqual(totals['c'], 45)
        self.assertEqual(totals['f'], 13)
        self.assertAlmostEqual(score, 0.0, places=5)
        
    def test_calculate_score_with_empty_meal_plan(self):
        """Test score calculation with empty meal plan"""
        meal_plan = []
        targets = {'p': 25, 'c': 45, 'f': 13}
        weights = Config.WEIGHTS
        penalties = Config.PENALTIES
        
        score, totals = self.finder._calculate_score(meal_plan, targets, weights, penalties)
        
        self.assertEqual(score, float('inf'))
        self.assertEqual(totals, {})
        
    def test_calculate_score_with_multiple_items(self):
        """Test score calculation with multiple items"""
        meal_plan = [
            {'p': 10, 'c': 20, 'f': 5},
            {'p': 15, 'c': 25, 'f': 8}
        ]
        targets = {'p': 25, 'c': 45, 'f': 13}
        weights = Config.WEIGHTS
        penalties = Config.PENALTIES
        
        score, totals = self.finder._calculate_score(meal_plan, targets, weights, penalties)
        
        self.assertEqual(totals['p'], 25)
        self.assertEqual(totals['c'], 45)
        self.assertEqual(totals['f'], 13)
        self.assertAlmostEqual(score, 0.0, places=5)
        
    def test_calculate_score_with_protein_deficit(self):
        """Test that protein deficit applies penalty"""
        meal_plan = [{'p': 10, 'c': 45, 'f': 13}]
        targets = {'p': 25, 'c': 45, 'f': 13}
        weights = Config.WEIGHTS
        penalties = Config.PENALTIES
        
        score, totals = self.finder._calculate_score(meal_plan, targets, weights, penalties)
        
        # Score should be positive due to protein deficit
        self.assertGreater(score, 0)
        
    def test_calculate_score_with_carb_excess(self):
        """Test that carb excess applies penalty"""
        meal_plan = [{'p': 25, 'c': 60, 'f': 13}]
        targets = {'p': 25, 'c': 45, 'f': 13}
        weights = Config.WEIGHTS
        penalties = Config.PENALTIES
        
        score, totals = self.finder._calculate_score(meal_plan, targets, weights, penalties)

        # Score should be positive due to carb excess
        self.assertGreater(score, 0)

    def test_get_numeric_value_tolerates_messy_labels(self):
        """Malformed labels return 0.0 or the leading number, never raise."""
        self.assertEqual(self.finder._get_numeric_value("< 1 g"), 1.0)
        self.assertEqual(self.finder._get_numeric_value("1.2.3 oz"), 1.2)
        self.assertEqual(self.finder._get_numeric_value("no digits here"), 0.0)
        self.assertEqual(self.finder._get_numeric_value("."), 0.0)


class TestResponseHandling(unittest.TestCase):
    """Menu-response gating and cache freshness."""

    def setUp(self):
        self.finder = MealFinder()

    def test_response_has_menu_rejects_empty(self):
        self.assertFalse(self.finder._response_has_menu(None))
        self.assertFalse(self.finder._response_has_menu({"errors": [{"message": "x"}]}))
        self.assertFalse(
            self.finder._response_has_menu({"data": {"diningCourtByName": None}})
        )
        self.assertFalse(
            self.finder._response_has_menu(
                {"data": {"diningCourtByName": {"dailyMenu": None}}}
            )
        )

    def test_response_has_menu_accepts_real_data(self):
        payload = {"data": {"diningCourtByName": {"dailyMenu": {"meals": []}}}}
        self.assertTrue(self.finder._response_has_menu(payload))

    def test_cache_past_date_never_stale(self):
        self.assertTrue(
            self.finder._cache_is_fresh({"version": Config.CACHE_VERSION}, "2000-01-01")
        )

    def test_cache_from_older_version_is_refetched(self):
        # Caches written before station components and meal hours were fetched
        # lack those fields, so they must not be reused even for past dates.
        self.assertFalse(self.finder._cache_is_fresh({}, "2000-01-01"))
        self.assertFalse(
            self.finder._cache_is_fresh({"version": Config.CACHE_VERSION - 1}, "2000-01-01")
        )

    def test_cache_today_needs_recent_timestamp(self):
        today, now, version = MealFinder._today(), MealFinder._now(), Config.CACHE_VERSION
        fresh = {"version": version, "timestamp": now.isoformat()}
        stale = {"version": version, "timestamp": (now - timedelta(hours=48)).isoformat()}
        self.assertTrue(self.finder._cache_is_fresh(fresh, today))
        self.assertFalse(self.finder._cache_is_fresh(stale, today))
        self.assertFalse(self.finder._cache_is_fresh({"version": version}, today))


class TestMealPeriodsAt(unittest.TestCase):
    """Time-of-day -> open meal periods."""

    def _at(self, weekday, hour, minute=0):
        # 2026-06-01 is a Monday; +offset walks the week.
        from datetime import datetime, timedelta

        monday = datetime(2026, 6, 1, hour, minute)
        return monday + timedelta(days=weekday)

    def test_weekday_lunch(self):
        serving, hint = MealFinder._meal_periods_at(self._at(2, 12, 30))
        self.assertIn("Lunch", serving)
        self.assertIsNone(hint)

    def test_before_open(self):
        serving, hint = MealFinder._meal_periods_at(self._at(2, 5))
        self.assertEqual(serving, [])
        self.assertIn("7 am", hint)

    def test_late_night(self):
        serving, hint = MealFinder._meal_periods_at(self._at(2, 22))
        self.assertEqual(serving, [])
        self.assertIn("tomorrow", hint)

    def test_weekend_brunch(self):
        serving, _ = MealFinder._meal_periods_at(self._at(5, 10))  # Saturday
        self.assertIn("Brunch", serving)

    def test_no_brunch_on_weekday(self):
        serving, _ = MealFinder._meal_periods_at(self._at(2, 10))  # Wednesday
        self.assertNotIn("Brunch", serving)

    def test_gap_between_breakfast_and_lunch(self):
        # Weekday breakfast ends at 10 (it was wrongly 11 here, and the page
        # says 10), so 10:30 is between meals and should point to lunch.
        serving, hint = MealFinder._meal_periods_at(self._at(2, 10, 30))
        self.assertEqual(serving, [])
        self.assertEqual(hint, "Lunch starts at 11 am")


class TestNormalizeDate(unittest.TestCase):
    def test_valid_passthrough(self):
        self.assertEqual(MealFinder.normalize_date("2026-06-15"), "2026-06-15")

    def test_empty_defaults_to_today(self):
        # "Today" at Purdue, not on the server's clock (Render runs on UTC).
        expected = datetime.now(PURDUE_TZ).strftime("%Y-%m-%d")
        self.assertEqual(MealFinder.normalize_date(None), expected)

    def test_bad_format_raises(self):
        with self.assertRaises(ValueError):
            MealFinder.normalize_date("06/15/2026")

    def test_far_future_raises(self):
        with self.assertRaises(ValueError):
            MealFinder.normalize_date("2099-01-01")


class TestConfig(unittest.TestCase):
    """Test cases for Config class"""
    
    def test_config_constants_exist(self):
        """Test that all required config constants exist"""
        self.assertIsNotNone(Config.WEIGHTS)
        self.assertIsNotNone(Config.PENALTIES)
        self.assertIsNotNone(Config.DINING_COURTS)
        self.assertIsNotNone(Config.MEAL_PERIODS)
        
    def test_dining_courts_list(self):
        """Test that dining courts list is complete"""
        expected_courts = ["Wiley", "Earhart", "Windsor", "Ford", "Hillenbrand"]
        self.assertEqual(Config.DINING_COURTS, expected_courts)
        
    def test_meal_periods_list(self):
        """Test that meal periods list is complete"""
        expected_periods = ["Breakfast", "Brunch", "Lunch", "Late Lunch", "Dinner"]
        self.assertEqual(Config.MEAL_PERIODS, expected_periods)
        
    def test_optimization_parameters(self):
        """Test optimization parameters are reasonable"""
        self.assertGreater(Config.ITERATIONS, 0)
        self.assertGreater(Config.FINAL_TEMP, 0)
        self.assertGreaterEqual(Config.RESTARTS, 1)
        self.assertLessEqual(Config.MIN_ITEMS, Config.INITIAL_ITEMS)
        self.assertLessEqual(Config.INITIAL_ITEMS, Config.MAX_ITEMS)


class TestInputValidation(unittest.TestCase):
    """Test cases for input validation in app.py"""
    
    def test_validate_targets_with_valid_input(self):
        """Test validation with valid target inputs"""
        from app import validate_targets
        
        targets = {'p': 40, 'c': 60, 'f': 20}
        valid, error = validate_targets(targets)
        
        self.assertTrue(valid)
        self.assertIsNone(error)
        
    def test_validate_targets_with_missing_macro(self):
        """Test validation with missing macro"""
        from app import validate_targets
        
        targets = {'p': 40, 'c': 60}  # Missing 'f'
        valid, error = validate_targets(targets)
        
        self.assertFalse(valid)
        self.assertIn("Missing required macro", error)
        
    def test_validate_targets_with_negative_value(self):
        """Test validation with negative value"""
        from app import validate_targets
        
        targets = {'p': -10, 'c': 60, 'f': 20}
        valid, error = validate_targets(targets)

        self.assertFalse(valid)
        self.assertIn("out of range", error)
        
    def test_validate_targets_with_excessive_value(self):
        """Test validation with excessively high value"""
        from app import validate_targets
        
        targets = {'p': 600, 'c': 60, 'f': 20}
        valid, error = validate_targets(targets)

        self.assertFalse(valid)
        self.assertIn("out of range", error)
        
    def test_validate_meal_periods_with_valid_input(self):
        """Test meal period validation with valid input"""
        from app import validate_meal_periods
        
        meal_periods = ["Lunch", "Dinner"]
        valid, error = validate_meal_periods(meal_periods)
        
        self.assertTrue(valid)
        self.assertIsNone(error)
        
    def test_validate_meal_periods_with_empty_list(self):
        """Test meal period validation with empty list"""
        from app import validate_meal_periods
        
        meal_periods = []
        valid, error = validate_meal_periods(meal_periods)
        
        self.assertFalse(valid)
        self.assertIn("at least one", error)
        
    def test_validate_meal_periods_with_invalid_period(self):
        """Test meal period validation with invalid period"""
        from app import validate_meal_periods
        
        meal_periods = ["Lunch", "Snack"]  # "Snack" is not valid
        valid, error = validate_meal_periods(meal_periods)
        
        self.assertFalse(valid)
        self.assertIn("Invalid meal period", error)


class TestFindMealEndpoint(unittest.TestCase):
    """HTTP-level behaviour of /api/find_meal."""

    def setUp(self):
        import app as app_module

        self.app_module = app_module
        self.client = app_module.app.test_client()
        # Use a stub engine so no live Purdue calls happen.
        self._real_get_engine = app_module.get_engine

        class _StubEngine:
            loaded = False

            def normalize_date(self, raw):
                return MealFinder.normalize_date(raw)

            def has_data(self, date_str):
                return self.loaded

            def ensure_loaded_async(self, date_str):
                self.loaded = True  # pretend the background load finished

            def find_best_meal(self, *a, **kw):
                return {
                    "court": "Wiley", "meal_name": "Lunch", "plan": [],
                    "totals": {"p": 0, "c": 0, "f": 0}, "date": kw.get("date_str"),
                }

            def find_meal_now(self, *a, **kw):
                return {"currently_serving": False, "next_meal_hint": "Dinner starts at 5 pm"}

        self.stub = _StubEngine()
        app_module.get_engine = lambda: self.stub

    def tearDown(self):
        self.app_module.get_engine = self._real_get_engine

    def _payload(self):
        return {
            "targets": {"p": 40, "c": 60, "f": 20},
            "meal_periods": ["Lunch"],
            "date": "2026-06-15",
        }

    def test_cold_start_returns_503(self):
        resp = self.client.post("/api/find_meal", json=self._payload())
        self.assertEqual(resp.status_code, 503)
        self.assertIn("loading", resp.get_json()["error"].lower())

    def test_second_call_succeeds(self):
        self.client.post("/api/find_meal", json=self._payload())  # warms the stub
        resp = self.client.post("/api/find_meal", json=self._payload())
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["court"], "Wiley")

    def test_non_json_body_is_400_not_500(self):
        resp = self.client.post(
            "/api/find_meal", data="not json", content_type="text/plain"
        )
        self.assertEqual(resp.status_code, 400)

    def test_invalid_date_is_400(self):
        bad = self._payload()
        bad["date"] = "15-06-2026"
        resp = self.client.post("/api/find_meal", json=bad)
        self.assertEqual(resp.status_code, 400)

    def _post_with(self, **overrides):
        self.stub.loaded = True
        payload = self._payload()
        payload.update(overrides)
        return self.client.post("/api/find_meal", json=payload)

    def test_exclusion_string_is_400(self):
        # A bare string would be iterated letter by letter and exclude nearly
        # every food on the menu.
        self.assertEqual(self._post_with(exclusion_list="eggs").status_code, 400)

    def test_too_many_exclusions_is_400(self):
        self.assertEqual(self._post_with(exclusion_list=["x"] * 51).status_code, 400)

    def test_non_object_targets_or_filters_are_400_not_500(self):
        self.assertEqual(self._post_with(targets="pcf").status_code, 400)
        self.assertEqual(self._post_with(dietary_filters=["Vegan"]).status_code, 400)
        self.assertEqual(self._post_with(meal_periods=[{"x": 1}]).status_code, 400)

    def test_numeric_strings_in_targets_are_accepted(self):
        resp = self._post_with(targets={"p": "40", "c": "60", "f": "20"})
        self.assertEqual(resp.status_code, 200)

    def test_now_mode_needs_no_date_or_meal_periods(self):
        payload = {"mode": "now", "targets": {"p": 40, "c": 60, "f": 20}}
        self.assertEqual(self.client.post("/api/find_meal", json=payload).status_code, 503)
        resp = self.client.post("/api/find_meal", json=payload)
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.get_json()["currently_serving"])

    def test_unknown_mode_is_400(self):
        self.assertEqual(self._post_with(mode="later").status_code, 400)

    def test_featured_endpoint_is_gone(self):
        # Replaced by mode "now", which uses the visitor's own targets.
        self.assertEqual(self.client.get("/api/featured").status_code, 404)

    def test_api_responses_are_not_cached(self):
        self.assertEqual(self._post_with().headers.get("Cache-Control"), "no-store")


class TestExclusionList(unittest.TestCase):
    """find_best_meal must drop items named in the exclusion list, matching
    case-insensitively on substrings."""

    def setUp(self):
        self.finder = MealFinder()
        date_str = "2026-06-15"
        self.date_str = date_str

        def _item(name, p, c, f):
            return {
                "name": name, "court": "Testville", "meal_name": "Lunch",
                "p": p, "c": c, "f": f, "serving_size": "Serving", "traits": [],
            }

        master = [
            _item("Scrambled Eggs", 12, 2, 10),
            _item("Grilled Chicken Breast", 30, 0, 4),
            _item("White Rice", 4, 45, 1),
            _item("Brown Rice", 5, 44, 2),
            _item("Steamed Broccoli", 3, 6, 0),
            _item("Snickerdoodle Cookie", 1, 18, 5),
        ]
        with self.finder.data_lock:
            self.finder.menu_by_date[date_str] = {"master": master}
        # Data is already in memory; skip the network load.
        self.finder.ensure_loaded = lambda *a, **kw: None

    def _plan_names(self, exclusion_list):
        result = self.finder.find_best_meal(
            {"p": 40, "c": 60, "f": 20},
            ["Lunch"],
            exclusion_list,
            {},
            date_str=self.date_str,
        )
        self.assertIsNotNone(result, "expected a meal plan")
        return [i["name"] for i in result["plan"]]

    def test_no_exclusions_can_use_any_item(self):
        names = self._plan_names([])
        self.assertTrue(names)

    def test_exact_name_is_excluded(self):
        names = self._plan_names(["White Rice"])
        self.assertNotIn("White Rice", names)

    def test_match_is_case_insensitive_and_substring(self):
        names = self._plan_names(["rice"])
        self.assertFalse(
            [n for n in names if "rice" in n.lower()],
            "both 'White Rice' and 'Brown Rice' should be gone",
        )

    def test_blank_entries_are_ignored(self):
        # A blank string must not exclude everything.
        names = self._plan_names(["   ", ""])
        self.assertTrue(names)


class TestSecurityHardening(unittest.TestCase):
    """The app must not serve source/secret files and must send safe headers."""

    def setUp(self):
        import app as app_module
        self.client = app_module.app.test_client()

    def test_source_and_secret_files_are_not_served(self):
        for path in ("/app.py", "/config.py", "/meal_finder_engine.py",
                     "/.env", "/render.yaml", "/test_file.py",
                     "/requirements.txt"):
            resp = self.client.get(path)
            self.assertEqual(resp.status_code, 404, f"{path} should not be reachable")

    def test_pages_are_served(self):
        for path in ("/", "/terms", "/privacy"):
            self.assertEqual(self.client.get(path).status_code, 200)

    def test_security_headers_present(self):
        h = self.client.get("/").headers
        self.assertEqual(h.get("X-Content-Type-Options"), "nosniff")
        self.assertEqual(h.get("X-Frame-Options"), "DENY")
        self.assertEqual(h.get("Cross-Origin-Opener-Policy"), "same-origin")
        self.assertIn("geolocation=()", h.get("Permissions-Policy", ""))
        self.assertIn("Strict-Transport-Security", h)
        csp = h.get("Content-Security-Policy", "")
        self.assertIn("default-src 'self'", csp)
        self.assertIn("frame-ancestors 'none'", csp)
        self.assertIn("object-src 'none'", csp)

    def test_csp_allows_no_inline_code_or_third_parties(self):
        csp = self.client.get("/").headers["Content-Security-Policy"]
        self.assertNotIn("unsafe-inline", csp)
        self.assertNotIn("http", csp)

    def test_pages_have_no_inline_scripts_or_styles(self):
        # Required by the CSP above: inline code would be blocked.
        import re
        for path in ("/", "/terms", "/privacy"):
            html = self.client.get(path).get_data(as_text=True)
            self.assertNotRegex(html, r"<script(?![^>]*\bsrc=)", path)
            self.assertNotIn("<style", html, path)
            self.assertNotRegex(html, r'\sstyle="', path)
            self.assertNotRegex(html, r"\son[a-z]+=", path)
            head = html.split("<body", 1)[0]
            self.assertFalse(re.search(r"https?://", head), f"{path} loads something from another origin")

    def test_static_assets_are_versioned_and_cached(self):
        html = self.client.get("/").get_data(as_text=True)
        self.assertNotIn("__ASSET_VERSION__", html)
        import re
        for url in re.findall(r'(?:href|src)="(/static/[^"]+)"', html):
            resp = self.client.get(url)
            self.assertEqual(resp.status_code, 200, url)
            self.assertIn("immutable", resp.headers["Cache-Control"], url)
            self.assertIn("?v=", url)

    def test_static_assets_are_gzipped_when_accepted(self):
        plain = self.client.get("/static/chart.umd.min.js")
        zipped = self.client.get("/static/chart.umd.min.js", headers={"Accept-Encoding": "gzip, br"})
        self.assertIsNone(plain.headers.get("Content-Encoding"))
        self.assertEqual(zipped.headers.get("Content-Encoding"), "gzip")
        self.assertLess(len(zipped.get_data()), len(plain.get_data()) / 2)
        self.assertIn("javascript", zipped.headers["Content-Type"])

    def test_only_allow_listed_static_files(self):
        for path in ("/static/app.py", "/static/.env", "/static/..%2Fapp.py", "/static/"):
            self.assertEqual(self.client.get(path).status_code, 404, path)

    def test_no_admin_route(self):
        self.assertEqual(self.client.get("/admin").status_code, 404)


def _appearance(name, p=0.0, c=0.0, f=0.0, cal=0.0, traits=(), components=None, label_only=False):
    """An ItemAppearance in the shape Purdue's v3 GraphQL API returns."""
    facts = None
    if p or c or f:
        value = (lambda v: None) if label_only else (lambda v: v)
        facts = [
            {"name": "Serving Size", "value": None, "label": "1 Serving"},
            {"name": "Calories", "value": value(cal), "label": f"{round(cal)}"},
            {"name": "Protein", "value": value(p), "label": f"{round(p)}g"},
            {"name": "Total Carbohydrate", "value": value(c), "label": f"{round(c)}g"},
            {"name": "Total fat", "value": value(f), "label": f"{round(f)}g"},
        ]
    return {
        "displayName": name,
        "item": {"traits": [{"name": t} for t in traits], "nutritionFacts": facts},
        "components": components or [],
    }


def _meal(name, items, station="Grill", start=None, end=None, status="OPEN"):
    return {"name": name, "status": status, "startTime": start, "endTime": end,
            "stations": [{"name": station, "items": items}]}


def _food(name, meal, p, c, f, court="Wiley", traits=()):
    return {"name": name, "court": court, "meal_name": meal, "p": p, "c": c, "f": f,
            "cal": 4 * p + 4 * c + 9 * f, "station": "Grill", "serving_size": "1 Serving",
            "traits": list(traits)}


class TestMenuParsing(unittest.TestCase):
    """Turning Purdue's GraphQL responses into foods."""

    def setUp(self):
        self.finder = MealFinder()

    def _parse(self, meals):
        response = {"data": {"diningCourtByName": {"name": "Testville", "dailyMenu": {"meals": meals}}}}
        return self.finder._parse_court("Testville", response, set())

    def test_station_components_are_offered_as_foods(self):
        # Regression: entries like "Salad Bar" or "La Fonda Station" have no
        # nutrition of their own. Their foods live in `components`, which were
        # never requested, so most of each day's menu was invisible.
        salad_bar = _appearance("Salad Bar", components=[
            _appearance("Hard Boiled Egg Slices", p=6.2, c=1.0, f=5.2),
            _appearance("Salad Mix", p=1.0, c=2.0, f=0.1),
        ])
        foods, _ = self._parse([_meal("Lunch", [salad_bar], station="Fresh Tastes")])
        self.assertEqual({f["name"] for f in foods}, {"Hard Boiled Egg Slices", "Salad Mix"})
        self.assertTrue(all(f["station"] == "Salad Bar" for f in foods))

    def test_query_requests_components_and_hours(self):
        query = MealFinder.GRAPHQL_QUERY
        for field in ("components", "startTime", "endTime", "value"):
            self.assertIn(field, query)

    def test_same_food_at_two_stations_is_listed_once(self):
        rice = lambda: _appearance("White Rice", p=4, c=45, f=1)
        meals = [{"name": "Lunch", "status": "OPEN", "startTime": None, "endTime": None,
                  "stations": [{"name": "A", "items": [rice()]}, {"name": "B", "items": [rice()]}]}]
        foods, _ = self._parse(meals)
        self.assertEqual([f["name"] for f in foods], ["White Rice"])

    def test_numeric_value_preferred_over_rounded_label(self):
        foods, _ = self._parse([_meal("Lunch", [_appearance("Chicken", p=27.8029, c=0.4, f=3.2)])])
        self.assertAlmostEqual(foods[0]["p"], 27.8)

    def test_label_used_when_value_missing(self):
        foods, _ = self._parse([_meal("Lunch", [_appearance("Chicken", p=28, c=1, f=3, label_only=True)])])
        self.assertEqual(foods[0]["p"], 28.0)

    def test_meal_hours_are_parsed_in_purdue_time(self):
        # 22:00 UTC is 5 pm at Purdue in November.
        _, hours = self._parse([_meal("Dinner", [_appearance("Steak", p=30, c=0, f=12)],
                                      start="2026-11-08T22:00:00+00:00",
                                      end="2026-11-09T02:00:00+00:00")])
        start = hours[("Testville", "Dinner")]["start"]
        self.assertEqual((start.hour, start.minute), (17, 0))
        self.assertEqual(str(start.tzinfo), Config.TIMEZONE)


class TestDietaryFilters(unittest.TestCase):
    def test_no_gluten_excludes_gluten_and_wheat(self):
        # Regression: the filter looked for a "Contains Gluten" trait, which
        # Purdue never uses (it tags "Gluten" and "Wheat"), so it removed nothing.
        for traits in (["Gluten"], ["Wheat"], ["Gluten", "Wheat", "Soy"]):
            self.assertFalse(MealFinder._passes_filters({"traits": traits}, {"No Gluten": True}))
        self.assertTrue(MealFinder._passes_filters({"traits": ["Soy"]}, {"No Gluten": True}))

    def test_vegetarian_accepts_vegan(self):
        self.assertTrue(MealFinder._passes_filters({"traits": ["Vegan"]}, {"Vegetarian": True}))
        self.assertFalse(MealFinder._passes_filters({"traits": []}, {"Vegetarian": True}))

    def test_vegan_requires_vegan(self):
        self.assertFalse(MealFinder._passes_filters({"traits": ["Vegetarian"]}, {"Vegan": True}))


class TestPlateRules(unittest.TestCase):
    """find_best_meal must return a plate someone can actually eat."""

    def setUp(self):
        self.finder = MealFinder()
        self.date_str = MealFinder._today()
        day = MealFinder._now().date()
        at = lambda h: datetime.combine(day, time(h), tzinfo=PURDUE_TZ)
        master = [
            _food("Lunch Chicken", "Lunch", 30, 0, 5),
            _food("Lunch Rice", "Lunch", 4, 45, 1),
            _food("Lunch Salad", "Lunch", 2, 5, 3),
            _food("Dinner Steak", "Dinner", 10, 15, 14),
            _food("Dinner Potato", "Dinner", 3, 30, 2),
            _food("Dinner Beans", "Dinner", 8, 20, 1),
        ]
        hours = {("Wiley", "Lunch"): {"start": at(11), "end": at(14), "status": "OPEN"}}
        with self.finder.data_lock:
            self.finder.menu_by_date[self.date_str] = {"master": master, "hours": hours}
        self.finder.ensure_loaded = lambda *a, **kw: None

    def _find(self, targets=None, meals=("Lunch", "Dinner")):
        return self.finder.find_best_meal(
            targets or {"p": 40, "c": 60, "f": 20}, list(meals), [], {}, date_str=self.date_str
        )

    def test_plate_never_mixes_meal_periods(self):
        # Regression: one court's lunch and dinner items were pooled, so plates
        # combined food served hours apart (18 of 30 real plates did).
        for _ in range(10):
            result = self._find()
            self.assertEqual({i["meal_name"] for i in result["plan"]}, {result["meal_name"]})

    def test_each_food_listed_once_with_a_serving_count(self):
        # Regression: the same food could appear twice as separate lines
        # ("Carved Ham, Carved Ham"). Extra servings are now one line.
        for _ in range(10):
            plan = self._find()["plan"]
            names = [i["name"] for i in plan]
            self.assertEqual(len(names), len(set(names)))
            for item in plan:
                self.assertIn(item["servings"], range(1, Config.MAX_SERVINGS + 1))

    def test_snapshot_items_are_not_mutated(self):
        self._find()
        self.assertTrue(all("servings" not in i for i in self.finder.menu_by_date[self.date_str]["master"]))

    def test_result_reports_hours_and_targets(self):
        result = self._find(meals=("Lunch",))
        self.assertEqual(result["hours"]["label"], "11 am to 2 pm")
        self.assertEqual(result["targets"], {"p": 40.0, "c": 60.0, "f": 20.0})
        self.assertIn("cal", result["totals"])

    def test_negligible_foods_are_not_offered(self):
        with self.finder.data_lock:
            self.finder.menu_by_date[self.date_str]["master"].append(
                _food("Arugula", "Lunch", 0.4, 0.6, 0.1)
            )
        for _ in range(5):
            self.assertNotIn("Arugula", [i["name"] for i in self._find(meals=("Lunch",))["plan"]])

    def test_options_hold_each_courts_best_plate_best_first(self):
        with self.finder.data_lock:
            self.finder.menu_by_date[self.date_str]["master"].extend([
                _food("Ford Turkey", "Lunch", 25, 30, 8, court="Ford"),
                _food("Ford Fries", "Lunch", 3, 40, 15, court="Ford"),
            ])
        result = self._find()
        options = result["options"]
        self.assertEqual(sorted(o["court"] for o in options), ["Ford", "Wiley"])
        self.assertEqual([o["score"] for o in options], sorted(o["score"] for o in options))
        self.assertEqual((result["court"], result["meal_name"]),
                         (options[0]["court"], options[0]["meal_name"]))

    def test_numeric_string_targets_do_not_crash(self):
        # validate_targets accepts "40"; the engine then subtracted a string.
        self.assertIsNotNone(self._find(targets={"p": "40", "c": "60", "f": "20"}))


class TestOptimizer(unittest.TestCase):
    """The optimizer should find the best plate, not merely a decent one."""

    def setUp(self):
        self.finder = MealFinder()

    @staticmethod
    def _brute_force(pool, targets):
        """Best score over every plate of MIN..MAX servings, <= MAX_SERVINGS per food."""
        score = MealFinder._make_scorer(targets, Config.WEIGHTS, Config.PENALTIES)
        best = float("inf")
        for k in range(Config.MIN_ITEMS, Config.MAX_ITEMS + 1):
            for combo in itertools.combinations_with_replacement(range(len(pool)), k):
                if max(combo.count(i) for i in set(combo)) > Config.MAX_SERVINGS:
                    continue
                best = min(best, score(sum(pool[i]["p"] for i in combo),
                                       sum(pool[i]["c"] for i in combo),
                                       sum(pool[i]["f"] for i in combo)))
        return best

    def _solve(self, pool, targets, seed=0):
        return self.finder._optimize_pool(
            pool, targets, Config.WEIGHTS, Config.PENALTIES, rng=random.Random(seed)
        )

    def test_near_optimal_on_random_menus(self):
        # The search is a heuristic. Measured on 120 random menus it found the
        # exact optimum in 92% of them, and missed by at most ~5 score points
        # (a few grams on one macro) otherwise. Hold it to that.
        rng = random.Random(42)
        trials, exact = 8, 0
        for trial in range(trials):
            pool = [{"name": f"item{i}", "p": round(rng.uniform(0, 30), 1),
                     "c": round(rng.uniform(0, 50), 1), "f": round(rng.uniform(0, 20), 1)}
                    for i in range(18)]
            targets = {"p": rng.choice([30, 40, 55]), "c": rng.choice([40, 60, 80]),
                       "f": rng.choice([15, 20, 30])}
            _, got, _ = self._solve(pool, targets, seed=trial)
            gap = got - self._brute_force(pool, targets)
            self.assertGreaterEqual(gap, -1e-9)  # can't beat the true optimum
            self.assertLess(gap, 5.0)
            exact += gap < 1e-6
        self.assertGreaterEqual(exact, trials - 2)

    def test_escapes_a_dominant_item(self):
        # One big item nearly covers the target alone, but three smaller items
        # hit it exactly. No single swap/add/remove leads from one plate to the
        # other, which is where the previous optimizer got stuck.
        pool = [
            {"name": "Big Nuggets", "p": 32.4, "c": 56.7, "f": 22.3},
            {"name": "Chicken", "p": 20, "c": 0, "f": 5},
            {"name": "Pasta", "p": 12, "c": 30, "f": 8},
            {"name": "Bread", "p": 8, "c": 30, "f": 7},
            {"name": "Lettuce", "p": 0.8, "c": 1, "f": 0.1},
            {"name": "Mushrooms", "p": 0.4, "c": 0.4, "f": 0},
        ]
        plan, score, _ = self._solve(pool, {"p": 40, "c": 60, "f": 20})
        self.assertAlmostEqual(score, 0.0, places=6)
        self.assertNotIn("Big Nuggets", [i["name"] for i in plan])

    def test_plate_size_limits_count_servings(self):
        rng = random.Random(3)
        pool = [{"name": f"i{i}", "p": rng.uniform(0, 5), "c": rng.uniform(0, 5),
                 "f": rng.uniform(0, 5)} for i in range(30)]
        servings = lambda plan: sum(i["servings"] for i in plan)
        plan, _, _ = self._solve(pool, {"p": 200, "c": 200, "f": 100})
        self.assertLessEqual(servings(plan), Config.MAX_ITEMS)
        plan, _, _ = self._solve(pool, {"p": 0, "c": 0, "f": 0})
        self.assertGreaterEqual(servings(plan), Config.MIN_ITEMS)

    def test_high_protein_target_uses_a_second_serving(self):
        # A 70 g protein target is out of reach with one serving of each food
        # here; two servings of chicken hit it exactly.
        pool = [
            {"name": "Chicken", "p": 30, "c": 0, "f": 3},
            {"name": "Rice", "p": 4, "c": 40, "f": 1},
            {"name": "Broccoli", "p": 3, "c": 6, "f": 0},
            {"name": "Apple", "p": 0.5, "c": 25, "f": 0.3},
        ]
        plan, score, totals = self._solve(pool, {"p": 67, "c": 46, "f": 7})
        chicken = next(i for i in plan if i["name"] == "Chicken")
        self.assertEqual(chicken["servings"], 2)
        self.assertAlmostEqual(score, 0.0, places=6)
        self.assertEqual(totals["p"], 67.0)

    def test_pool_too_small(self):
        plan, score, totals = self._solve([{"name": "only", "p": 1, "c": 1, "f": 1}],
                                          {"p": 40, "c": 60, "f": 20})
        self.assertIsNone(plan)


class TestMealNow(unittest.TestCase):
    """"Right now" optimizes over the meals open at this moment, by Purdue's
    published per-court hours, in Purdue time."""

    def setUp(self):
        self.finder = MealFinder()
        self.day = MealFinder._now().date()
        self.date_str = self.day.strftime("%Y-%m-%d")
        master = [
            _food("Grilled Chicken", "Dinner", 30, 0, 5),
            _food("Brown Rice", "Dinner", 5, 44, 2),
            _food("Steamed Broccoli", "Dinner", 3, 6, 0, traits=["Vegan", "Vegetarian"]),
            _food("Turkey Sandwich", "Lunch", 25, 30, 8, court="Ford"),
            _food("Pasta Salad", "Lunch", 6, 35, 7, court="Ford"),
            _food("Pot Roast", "Dinner", 28, 8, 12, court="Earhart"),
            _food("Mashed Potatoes", "Dinner", 4, 35, 9, court="Earhart"),
        ]
        hours = {
            ("Wiley", "Dinner"): {"start": self._at(17), "end": self._at(21), "status": "OPEN"},
            ("Ford", "Lunch"): {"start": self._at(11), "end": self._at(14), "status": "OPEN"},
            ("Earhart", "Dinner"): {"start": self._at(17), "end": self._at(20), "status": "OPEN"},
        }
        with self.finder.data_lock:
            self.finder.menu_by_date[self.date_str] = {"master": master, "hours": hours}
        self.finder.ensure_loaded = lambda *a, **kw: None

    def _at(self, hour, minute=0):
        return datetime.combine(self.day, time(hour, minute), tzinfo=PURDUE_TZ)

    def _now(self, at, **kw):
        return self.finder.find_meal_now({"p": 40, "c": 50, "f": 10}, now=at, **kw)

    def test_uses_only_meals_open_now(self):
        result = self._now(self._at(18))
        self.assertTrue(result["currently_serving"])
        self.assertEqual(result["meal_name"], "Dinner")
        courts = {o["court"] for o in result["options"]}
        self.assertEqual(courts, {"Wiley", "Earhart"})  # Ford lunch is over
        wiley = next(o for o in result["options"] if o["court"] == "Wiley")
        self.assertEqual(wiley["hours"]["label"], "5 to 9 pm")

    def test_court_that_closed_early_is_skipped(self):
        result = self._now(self._at(20, 30))  # Earhart closes at 8, Wiley at 9
        self.assertEqual([o["court"] for o in result["options"]], ["Wiley"])

    def test_uses_the_visitors_targets_and_filters(self):
        # Regression: the old "featured plate" ignored targets entirely and
        # served 27 g protein / 125 g carbs. Vegan here leaves only broccoli.
        result = self._now(self._at(18), dietary_filters={"Vegan": True})
        self.assertIsNone(result)  # one vegan food can't make a 2-item plate

    def test_between_meals_points_to_next_opening(self):
        result = self._now(self._at(15))
        self.assertFalse(result["currently_serving"])
        self.assertEqual(result["next_meal_hint"], "Dinner starts at 5 pm")

    def test_after_last_meal(self):
        result = self._now(self._at(22))
        self.assertFalse(result["currently_serving"])
        self.assertIn("tomorrow", result["next_meal_hint"])

    def test_server_clock_in_utc_still_uses_purdue_time(self):
        # Regression: Render runs on UTC, so at 6 pm Purdue time the server's
        # naive clock read 11 pm and "Right now" said nothing was being served.
        result = self._now(self._at(18).astimezone(timezone.utc))
        self.assertTrue(result["currently_serving"])


class TestTimezone(unittest.TestCase):
    def test_now_is_purdue_time(self):
        self.assertEqual(str(MealFinder._now().tzinfo), Config.TIMEZONE)

    def test_utc_evening_is_still_the_same_day_at_purdue(self):
        # 01:30 UTC on Nov 9 is 8:30 pm on Nov 8 at Purdue.
        t = MealFinder._as_purdue_time("2026-11-09T01:30:00+00:00")
        self.assertEqual((t.month, t.day, t.hour, t.minute), (11, 8, 20, 30))

    def test_naive_timestamps_are_read_as_purdue_time(self):
        t = MealFinder._as_purdue_time("2026-11-08T12:00:00")
        self.assertEqual((t.hour, str(t.tzinfo)), (12, Config.TIMEZONE))


class TestSnapshotFreshness(unittest.TestCase):
    """In-memory menus used to be kept forever, even when empty or partial."""

    def setUp(self):
        self.finder = MealFinder()
        self.now = MealFinder._now()

    def _snap(self, age, complete=True, master=("food",)):
        return {"master": list(master), "complete": complete, "loaded_at": self.now - age}

    def test_partial_load_is_retried_after_a_few_minutes(self):
        retry = timedelta(minutes=Config.INCOMPLETE_RETRY_MINUTES)
        self.assertFalse(self.finder._snapshot_is_stale(self._snap(retry / 2, complete=False), "2000-01-01"))
        self.assertTrue(self.finder._snapshot_is_stale(self._snap(retry * 2, complete=False), "2000-01-01"))

    def test_empty_menu_is_retried(self):
        retry = timedelta(minutes=Config.INCOMPLETE_RETRY_MINUTES)
        self.assertTrue(self.finder._snapshot_is_stale(self._snap(retry * 2, master=()), MealFinder._today()))

    def test_complete_past_menu_never_expires(self):
        self.assertFalse(self.finder._snapshot_is_stale(self._snap(timedelta(days=30)), "2000-01-01"))

    def test_todays_menu_expires_after_ttl(self):
        ttl = timedelta(hours=Config.CACHE_TTL_HOURS)
        today = MealFinder._today()
        self.assertFalse(self.finder._snapshot_is_stale(self._snap(ttl / 2), today))
        self.assertTrue(self.finder._snapshot_is_stale(self._snap(ttl * 2), today))

    def test_preloads_today_then_tomorrow(self):
        loaded, done = [], __import__("threading").Event()

        def fake_load(date_str, refresh=False):
            loaded.append(date_str)
            if len(loaded) == 2:
                done.set()

        self.finder._load_menu_for_date = fake_load
        self.finder.start_background_loaders()
        self.assertTrue(done.wait(2))
        today = self.now.date()
        self.assertEqual(loaded, [today.strftime("%Y-%m-%d"),
                                  (today + timedelta(days=1)).strftime("%Y-%m-%d")])

    def test_stale_menu_is_served_while_refreshing_in_background(self):
        today = MealFinder._today()
        stale = self._snap(timedelta(hours=Config.CACHE_TTL_HOURS * 2))
        with self.finder.data_lock:
            self.finder.menu_by_date[today] = stale
        calls = []
        self.finder._start_background_load = lambda d, refresh=False: calls.append((d, refresh))
        self.finder.ensure_loaded(today)
        self.assertEqual(calls, [(today, True)])
        self.assertIs(self.finder.menu_by_date[today], stale)


if __name__ == '__main__':
    unittest.main()
