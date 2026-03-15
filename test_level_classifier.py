"""
Unit tests for the level classification module.

Run with:
    cd 86d-api && python -m pytest test_level_classifier.py -v
"""
import pytest
from helpers import decimal_to_level, classify_level


# ── decimal_to_level (hard thresholds, no hysteresis) ────────────────────────

class TestDecimalToLevel:
    def test_full(self):
        assert decimal_to_level(1.0) == "almost_full"
        assert decimal_to_level(0.875) == "almost_full"
        assert decimal_to_level(0.90) == "almost_full"

    def test_three_quarters(self):
        assert decimal_to_level(0.625) == "3/4"
        assert decimal_to_level(0.75)  == "3/4"
        assert decimal_to_level(0.874) == "3/4"

    def test_half(self):
        assert decimal_to_level(0.375) == "half"
        assert decimal_to_level(0.5)   == "half"
        assert decimal_to_level(0.624) == "half"

    def test_quarter(self):
        assert decimal_to_level(0.125) == "1/4"
        assert decimal_to_level(0.25)  == "1/4"
        assert decimal_to_level(0.374) == "1/4"

    def test_empty(self):
        assert decimal_to_level(0.0)   == "empty"
        assert decimal_to_level(0.124) == "empty"

    # Exact boundary values
    def test_exact_3_4_boundary(self):
        assert decimal_to_level(0.625) == "3/4"

    def test_just_below_3_4_boundary(self):
        assert decimal_to_level(0.6249) == "half"

    def test_exact_half_boundary(self):
        assert decimal_to_level(0.375) == "half"

    def test_just_below_half_boundary(self):
        assert decimal_to_level(0.3749) == "1/4"


# ── classify_level — no previous level (falls back to hard thresholds) ───────

class TestClassifyLevelNoPrevious:
    def test_no_previous_returns_hard_classification(self):
        assert classify_level(0.63)  == "3/4"
        assert classify_level(0.62)  == "half"
        assert classify_level(0.5)   == "half"
        assert classify_level(0.375) == "half"
        assert classify_level(0.374) == "1/4"

    def test_no_hysteresis_flag_ignores_previous(self):
        # Even with previous provided, hysteresis=False should use hard threshold.
        assert classify_level(0.63, previous_level="half", hysteresis=False) == "3/4"
        assert classify_level(0.62, previous_level="3/4", hysteresis=False) == "half"


# ── classify_level — hysteresis / deadband ────────────────────────────────────

class TestClassifyLevelHysteresis:
    """
    Deadband default = ±0.03 around each boundary.
    3/4-half boundary = 0.625  →  deadband zone: [0.595, 0.655]
    half-1/4 boundary = 0.375  →  deadband zone: [0.345, 0.405]
    """

    # --- 3/4 / half boundary (0.625) ---

    def test_near_boundary_sticks_with_previous_half(self):
        # 0.63 is within ±0.03 of 0.625; previous was 'half' → stays 'half'
        assert classify_level(0.63, previous_level="half") == "half"

    def test_near_boundary_sticks_with_previous_three_quarters(self):
        # 0.62 is within ±0.03 of 0.625; previous was '3/4' → stays '3/4'
        assert classify_level(0.62, previous_level="3/4") == "3/4"

    def test_clearly_above_boundary_crosses_to_three_quarters(self):
        # 0.66 > 0.655 (outside deadband); previous was 'half' → crosses to '3/4'
        assert classify_level(0.66, previous_level="half") == "3/4"

    def test_clearly_below_boundary_crosses_to_half(self):
        # 0.59 < 0.595 (outside deadband); previous was '3/4' → crosses to 'half'
        assert classify_level(0.59, previous_level="3/4") == "half"

    # --- half / 1/4 boundary (0.375) ---

    def test_near_half_quarter_boundary_sticks_with_half(self):
        assert classify_level(0.385, previous_level="half") == "half"

    def test_near_half_quarter_boundary_sticks_with_quarter(self):
        assert classify_level(0.365, previous_level="1/4") == "1/4"

    def test_clearly_above_half_quarter_boundary_crosses(self):
        assert classify_level(0.41, previous_level="1/4") == "half"

    def test_clearly_below_half_quarter_boundary_crosses(self):
        assert classify_level(0.34, previous_level="half") == "1/4"

    # --- Flip-flop prevention: repeated readings around 0.58–0.64 ---

    def test_flip_flop_sequence_stays_half(self):
        """A stable half-full bottle with readings 0.58–0.64 should not flip."""
        readings = [0.60, 0.61, 0.62, 0.63, 0.60, 0.64, 0.61, 0.62]
        level = "half"
        for r in readings:
            level = classify_level(r, previous_level=level)
        assert level == "half", f"Expected 'half' after sequence, got '{level}'"

    def test_flip_flop_sequence_stays_three_quarters(self):
        """Same sequence but starting from '3/4' should stay '3/4'."""
        readings = [0.63, 0.62, 0.61, 0.64, 0.60, 0.63, 0.62]
        level = "3/4"
        for r in readings:
            level = classify_level(r, previous_level=level)
        assert level == "3/4", f"Expected '3/4' after sequence, got '{level}'"

    def test_sustained_evidence_does_cross_boundary(self):
        """A reading well past the boundary+deadband should cross even with prior history."""
        level = "half"
        # A genuinely 3/4 bottle reads consistently high
        for r in [0.70, 0.72, 0.68]:
            level = classify_level(r, previous_level=level)
        assert level == "3/4"

    # --- Custom deadband ---

    def test_custom_deadband_wider(self):
        # With deadband=0.10, even 0.67 (0.625+0.045 < 0.625+0.10) is in zone
        assert classify_level(0.67, previous_level="half", deadband=0.10) == "half"

    def test_custom_deadband_zero_no_hysteresis_effect(self):
        # deadband=0 means any crossing is accepted regardless of previous
        assert classify_level(0.63, previous_level="half", deadband=0.0) == "3/4"

    # --- Clamping ---

    def test_clamped_above_1(self):
        assert classify_level(1.5) == "almost_full"

    def test_clamped_below_0(self):
        assert classify_level(-0.1) == "empty"


# ── Confidence / needs_rescan signal (logic only, no HTTP) ────────────────────

class TestNeedsRescanLogic:
    """
    The API sets needs_rescan=True when confidence < CONFIDENCE_THRESHOLD
    or levelReadable is False.  We replicate that logic here for clarity.
    """
    THRESHOLD = 0.35

    def _needs_rescan(self, confidence: float, level_readable: bool) -> bool:
        return not level_readable or confidence < self.THRESHOLD

    def test_high_confidence_readable(self):
        assert self._needs_rescan(0.9, True) is False

    def test_low_confidence_triggers_rescan(self):
        assert self._needs_rescan(0.20, True) is True

    def test_exactly_at_threshold_does_not_trigger(self):
        assert self._needs_rescan(self.THRESHOLD, True) is False

    def test_just_below_threshold_triggers(self):
        assert self._needs_rescan(self.THRESHOLD - 0.001, True) is True

    def test_unreadable_triggers_regardless_of_confidence(self):
        assert self._needs_rescan(0.95, False) is True

    def test_both_bad_triggers(self):
        assert self._needs_rescan(0.10, False) is True
