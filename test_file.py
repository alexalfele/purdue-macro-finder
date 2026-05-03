"""
Unit tests for the Purdue Macro Finder
Run with: python -m pytest test_meal_finder.py
"""

import unittest
from unittest.mock import Mock, patch
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
        
    def test_ensure_current_date_returns_false_when_current(self):
        """Test that _ensure_current_date returns False when date is current"""
        # Date is set to current in __init__
        result = self.finder._ensure_current_date()
        self.assertFalse(result)
        
    def test_get_top_protein_foods_with_empty_list(self):
        """Test getting top protein foods with empty master list"""
        self.finder.master_item_list = []
        result = self.finder.get_top_protein_foods(10)
        self.assertEqual(len(result), 0)
        
    def test_get_top_protein_foods_filters_low_calorie(self):
        """Test that low calorie items are filtered out"""
        self.finder.master_item_list = [
            {'name': 'Low Cal Item', 'p': 5, 'c': 5, 'f': 0},  # 40 calories
            {'name': 'High Protein', 'p': 30, 'c': 10, 'f': 5},  # 205 calories
        ]
        result = self.finder.get_top_protein_foods(10)
        
        # Should only include the high calorie item
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['name'], 'High Protein')


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
        expected_periods = ["Breakfast", "Lunch", "Dinner", "Late Night"]
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
        self.assertIn("non-negative", error)
        
    def test_validate_targets_with_excessive_value(self):
        """Test validation with excessively high value"""
        from app import validate_targets
        
        targets = {'p': 600, 'c': 60, 'f': 20}
        valid, error = validate_targets(targets)
        
        self.assertFalse(valid)
        self.assertIn("unreasonably high", error)
        
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


if __name__ == '__main__':
    unittest.main()
