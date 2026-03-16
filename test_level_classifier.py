"""
Unit tests for the level classification module.

Run with:
    cd 86d-api && python -m pytest test_level_classifier.py -v
"""
import pytest
from helpers import decimal_to_level, classify_level, smooth_level


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


# ── confidence-aware stickiness ───────────────────────────────────────────────

class TestConfidenceAwareStickiness:
    """
    Low confidence widens the effective deadband so borderline reads stick
    harder to the previous label.

    Default deadband = 0.03.  At confidence=0.0 it doubles to 0.06.
    3/4-half boundary = 0.625  →  at conf=0.0 zone becomes [0.565, 0.685].
    """

    def test_high_confidence_crosses_boundary_normally(self):
        # 0.66 is outside default deadband (0.655); high confidence → cross
        assert classify_level(0.66, previous_level="half", confidence=0.9) == "3/4"

    def test_low_confidence_keeps_previous_near_boundary(self):
        # 0.66 at conf=0.0 → effective deadband=0.06 → zone=[0.565, 0.685]
        # 0.66 is inside that zone, so previous label sticks
        assert classify_level(0.66, previous_level="half", confidence=0.0) == "half"

    def test_low_confidence_still_crosses_when_far_from_boundary(self):
        # 0.75 is far above 0.625 regardless of confidence → should cross
        assert classify_level(0.75, previous_level="half", confidence=0.0) == "3/4"

    def test_mid_confidence_partial_widening(self):
        # confidence=0.25 → scale = 2 - 0.5 = 1.5 → effective deadband = 0.045
        # zone = [0.580, 0.670].  0.66 is inside → sticks.
        assert classify_level(0.66, previous_level="half", confidence=0.25) == "half"

    def test_confidence_at_threshold_boundary_no_widening(self):
        # confidence=0.5 → scale=1.0 → effective deadband unchanged at 0.03
        # 0.66 > 0.655 → outside deadband → crosses
        assert classify_level(0.66, previous_level="half", confidence=0.5) == "3/4"

    def test_confidence_none_behaves_as_before(self):
        # When confidence not supplied, behaviour is identical to pre-change
        assert classify_level(0.63, previous_level="half") == "half"
        assert classify_level(0.66, previous_level="half") == "3/4"

    def test_repeated_low_confidence_sequence_near_625_flips_at_most_once(self):
        """Acceptance test: 10 reads near 0.625 with low confidence flip ≤ 1 time."""
        readings = [0.61, 0.64, 0.62, 0.63, 0.60, 0.65, 0.62, 0.64, 0.61, 0.63]
        level = "half"
        flips = 0
        for r in readings:
            new = classify_level(r, previous_level=level, confidence=0.3)
            if new != level:
                flips += 1
            level = new
        assert flips <= 1, f"Expected ≤1 flip near 0.625 boundary, got {flips}"


# ── smooth_level ──────────────────────────────────────────────────────────────

class TestSmoothLevel:
    def test_single_reading_returns_itself(self):
        assert smooth_level([0.6]) == 0.6

    def test_median_of_three_odd(self):
        assert smooth_level([0.5, 0.9, 0.6]) == 0.6

    def test_median_of_three_even_window_two(self):
        # window=2 → take last 2: [0.6, 0.9] → median = (0.6+0.9)/2 = 0.75
        assert smooth_level([0.5, 0.6, 0.9], window=2) == 0.75

    def test_outlier_does_not_move_median(self):
        # Single glare spike (0.95) in a stable half-full sequence
        assert smooth_level([0.50, 0.52, 0.95]) == 0.52

    def test_empty_list_returns_zero(self):
        assert smooth_level([]) == 0.0

    def test_clamps_above_one(self):
        assert smooth_level([1.1, 1.2, 1.3]) == 1.0

    def test_clamps_below_zero(self):
        assert smooth_level([-0.1, -0.2, -0.3]) == 0.0

    def test_window_larger_than_list_uses_all(self):
        # Only 2 readings available, window=5 → uses both
        assert smooth_level([0.4, 0.6], window=5) == 0.5

    def test_uses_only_last_n_readings(self):
        # History: [0.2, 0.2, 0.6, 0.6, 0.6] → last 3: [0.6, 0.6, 0.6]
        assert smooth_level([0.2, 0.2, 0.6, 0.6, 0.6], window=3) == 0.6

    def test_stabilization_prevents_bucket_flip_on_spike(self):
        """A glare spike (0.95) smoothed against two 0.5 reads stays near half."""
        smoothed = smooth_level([0.50, 0.50, 0.95], window=3)
        # Median is 0.50 — well below the 0.625 boundary
        assert smoothed < 0.625
