"""
Unit tests for the Purdue Macro Finder
Run with: python -m pytest test_file.py
"""

import unittest
from meal_finder_engine import MealFinder
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
        self.assertTrue(self.finder._cache_is_fresh({}, "2000-01-01"))

    def test_cache_today_needs_recent_timestamp(self):
        from datetime import datetime, timedelta

        today = datetime.now().strftime("%Y-%m-%d")
        fresh = {"timestamp": datetime.now().isoformat()}
        stale = {"timestamp": (datetime.now() - timedelta(hours=48)).isoformat()}
        self.assertTrue(self.finder._cache_is_fresh(fresh, today))
        self.assertFalse(self.finder._cache_is_fresh(stale, today))
        self.assertFalse(self.finder._cache_is_fresh({}, today))


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


class TestNormalizeDate(unittest.TestCase):
    def test_valid_passthrough(self):
        self.assertEqual(MealFinder.normalize_date("2026-06-15"), "2026-06-15")

    def test_empty_defaults_to_today(self):
        from datetime import date

        self.assertEqual(MealFinder.normalize_date(None), date.today().strftime("%Y-%m-%d"))

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
        self.assertGreater(Config.INITIAL_TEMP, 0)
        self.assertLess(Config.COOLING_RATE, 1)
        self.assertGreater(Config.COOLING_RATE, 0)
        self.assertGreater(Config.ITERATIONS, 0)


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
        self.assertIn("Strict-Transport-Security", h)
        self.assertIn("default-src 'self'", h.get("Content-Security-Policy", ""))
        self.assertIn("frame-ancestors 'none'", h.get("Content-Security-Policy", ""))

    def test_no_admin_route(self):
        self.assertEqual(self.client.get("/admin").status_code, 404)


if __name__ == '__main__':
    unittest.main()
