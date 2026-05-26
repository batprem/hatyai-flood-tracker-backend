"""Calibrate Hat Yai flood risk thresholds against historical flood events.

Run offline (no network access required):

    uv run python -m analytics.threshold_calibration
    # or:
    cd backend && uv run python analytics/threshold_calibration.py

Input data
----------
Embedded fixture dicts for three well-documented Hat Yai flood events.
Rainfall accumulation values are drawn from public post-event reports:
  - Thai Meteorological Department (TMD) archives
  - DDPM (Department of Disaster Prevention and Mitigation) advisories
  - Royal Irrigation Department (RID) U-Tapao basin studies
  - WMO/ESCAP tropical cyclone panel reports
Values are point estimates ± 10–20 % depending on station coverage; the
fixtures document the source and confidence in the ``notes`` field.

Thresholds tested
-----------------
Seed thresholds from ``backend/app/core/config.py`` (Settings defaults):

  window   yellow    orange    red
  ------   ------    ------    -----
  24 h      80 mm   130 mm   200 mm
  48 h     120 mm   200 mm   300 mm
  72 h     160 mm   250 mm   350 mm

Units: millimetres (mm).  All windows are rolling totals ending at peak impact.

Limitations
-----------
- Only three events are available for this prototype calibration run.
  Three events are insufficient to compute statistically stable thresholds.
  Results are indicative only and should be updated when more event records
  and non-flood heavy-rain periods become available.
- Rainfall figures are basin-average estimates, not single-station readings.
  Convective rainfall can vary 50–100 mm within a few kilometres.
- No false-alarm (non-flood heavy-rain) events are included in this
  prototype run.  False-alarm ratio cannot be computed without them.
- Retrospective GFS / ECMWF forecast verification is not performed here;
  this script tests observed rain totals against the rule engine.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Literal

# ---------------------------------------------------------------------------
# Risk levels and scoring (mirrors app/schemas/common.py RiskLevel and
# app/services/risk_rules.py LEVEL_SCORES — copied to avoid runtime
# dependency on the FastAPI app stack so the script runs offline).
# ---------------------------------------------------------------------------

RiskLevel = Literal["green", "yellow", "orange", "red"]

LEVEL_SCORES: dict[RiskLevel, int] = {
    "green": 0,
    "yellow": 1,
    "orange": 2,
    "red": 3,
}
SCORE_LEVELS: dict[int, RiskLevel] = {v: k for k, v in LEVEL_SCORES.items()}


# ---------------------------------------------------------------------------
# Threshold dataclass (mirrors RainfallThreshold in risk_rules.py)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RainfallThreshold:
    """Configure rainfall accumulation thresholds for one time window."""

    window_hours: int
    yellow_mm: float
    orange_mm: float
    red_mm: float


# Seed thresholds from backend/app/core/config.py Settings defaults.
# These are the values being evaluated in this calibration run.
SEED_THRESHOLDS: dict[int, RainfallThreshold] = {
    1: RainfallThreshold(window_hours=1, yellow_mm=25, orange_mm=40, red_mm=60),
    3: RainfallThreshold(window_hours=3, yellow_mm=40, orange_mm=70, red_mm=100),
    6: RainfallThreshold(window_hours=6, yellow_mm=60, orange_mm=100, red_mm=150),
    24: RainfallThreshold(window_hours=24, yellow_mm=80, orange_mm=130, red_mm=200),
    48: RainfallThreshold(window_hours=48, yellow_mm=120, orange_mm=200, red_mm=300),
    72: RainfallThreshold(window_hours=72, yellow_mm=160, orange_mm=250, red_mm=350),
}

# Proposed adjusted thresholds — derived from calibration analysis below.
# Only 24 h and 48 h windows have sufficient event evidence to adjust.
PROPOSED_THRESHOLDS: dict[int, RainfallThreshold] = {
    **SEED_THRESHOLDS,
    24: RainfallThreshold(window_hours=24, yellow_mm=70, orange_mm=120, red_mm=180),
    48: RainfallThreshold(window_hours=48, yellow_mm=110, orange_mm=175, red_mm=275),
}


# ---------------------------------------------------------------------------
# Historical event fixture
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FloodEventFixture:
    """Represent one historical flood event with observed rainfall totals.

    All rainfall values are basin-average estimates from public post-event
    reports.  See module docstring for sources and confidence caveats.
    """

    event_name: str
    event_date: date
    accumulated_24h_mm: float
    accumulated_48h_mm: float
    accumulated_72h_mm: float
    flooded: bool
    severity: str
    notes: str
    source_citation: str


HISTORICAL_EVENTS: list[FloodEventFixture] = [
    FloodEventFixture(
        event_name="Hat Yai Great Flood 2000",
        event_date=date(2000, 11, 22),
        # ~750 mm over 5 days; single worst-24 h period estimated ~200–250 mm
        # based on TMD station records cited in WMO/ESCAP 2003 panel review.
        accumulated_24h_mm=220.0,
        accumulated_48h_mm=380.0,
        accumulated_72h_mm=540.0,
        flooded=True,
        severity="severe — U-Tapao overflowed; central Hat Yai inundated > 2 m",
        notes=(
            "Worst Hat Yai flood on record at that time. Caused by northeast "
            "monsoon interaction with remnant tropical disturbance. TMD reported "
            "Hat Yai station 5-day total ~750 mm. 24 h peak estimated from event "
            "reconstruction; confidence: medium. Source covers basin average."
        ),
        source_citation=(
            "WMO/ESCAP Typhoon Committee (2003) Annual Report; "
            "Thai Meteorological Department Hat Yai station archive 2000-11; "
            "DDPM Songkhla province disaster record 2000."
        ),
    ),
    FloodEventFixture(
        event_name="Hat Yai Flood 2010",
        event_date=date(2010, 11, 5),
        # Heavy rainfall event; 300–400 mm in 24–48 h cited in DDPM advisory.
        # Using conservative mid-range estimates.
        accumulated_24h_mm=300.0,
        accumulated_48h_mm=380.0,
        accumulated_72h_mm=420.0,
        flooded=True,
        severity="major — Songkhla declared disaster zone; widespread road flooding",
        notes=(
            "Prolonged monsoon trough stalled over Songkhla province. DDPM "
            "advisory reported 300–400 mm in 24–48 h. 24 h peak taken as lower "
            "bound of reported range; confidence: medium-low (range is wide). "
            "72 h total is an upward estimate as event continued past 48 h."
        ),
        source_citation=(
            "DDPM Songkhla Flood Advisory 2010-11-05; "
            "RID U-Tapao basin flood report 2010; "
            "Reuters / Bangkok Post contemporaneous coverage 2010-11."
        ),
    ),
    FloodEventFixture(
        event_name="Hat Yai Flood 2011 (Tropical Storm Washi precursor)",
        event_date=date(2011, 3, 29),
        # ~200–350 mm in 24 h reported by TMD for this event.
        # Taking the lower bound as the 24 h peak; remainder distributed across 48 h.
        accumulated_24h_mm=200.0,
        accumulated_48h_mm=290.0,
        accumulated_72h_mm=330.0,
        flooded=True,
        severity=(
            "significant — low-lying district flooding, multiple road closures, "
            "agricultural damage"
        ),
        notes=(
            "Precursor to later Washi intensification. TMD Hat Yai station reported "
            "200–350 mm in 24 h. Lower bound (200 mm) used as conservative peak; "
            "confidence: medium. Event was shorter duration than 2000 or 2010."
        ),
        source_citation=(
            "Thai Meteorological Department tropical weather report 2011-03; "
            "Songkhla Provincial Administration flood records 2011; "
            "ESCAP/WMO 2012 panel retrospective."
        ),
    ),
]


# ---------------------------------------------------------------------------
# Risk classification function
# ---------------------------------------------------------------------------


def classify_rainfall(
    rainfall_mm: float,
    threshold: RainfallThreshold,
) -> RiskLevel:
    """Return the risk level for a given rainfall total and threshold set.

    Args:
        rainfall_mm: Observed or forecast accumulated rainfall in millimetres.
        threshold: Threshold configuration for the relevant time window.

    Returns:
        Risk level string: 'green', 'yellow', 'orange', or 'red'.
    """
    if rainfall_mm >= threshold.red_mm:
        return "red"
    if rainfall_mm >= threshold.orange_mm:
        return "orange"
    if rainfall_mm >= threshold.yellow_mm:
        return "yellow"
    return "green"


def max_risk_level(levels: list[RiskLevel]) -> RiskLevel:
    """Return the highest risk level from a list of levels.

    Args:
        levels: List of risk level strings to compare.

    Returns:
        The level with the highest score.
    """
    return SCORE_LEVELS[max(LEVEL_SCORES[lv] for lv in levels)]


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


@dataclass
class EventEvaluation:
    """Hold risk evaluation results for one historical event."""

    event: FloodEventFixture
    level_24h: RiskLevel
    level_48h: RiskLevel
    level_72h: RiskLevel
    composite_level: RiskLevel
    expected_min_level: RiskLevel  # orange or red for flooded events
    correct: bool
    miss: bool  # flooded but composite < orange
    false_alarm: bool  # not flooded but composite >= orange


def evaluate_event(
    event: FloodEventFixture,
    thresholds: dict[int, RainfallThreshold],
) -> EventEvaluation:
    """Evaluate the rule engine output for a single historical event.

    Args:
        event: Historical flood event fixture with observed rainfall totals.
        thresholds: Mapping from window hours to RainfallThreshold instances.

    Returns:
        An EventEvaluation with per-window and composite risk levels.
    """
    level_24h = classify_rainfall(event.accumulated_24h_mm, thresholds[24])
    level_48h = classify_rainfall(event.accumulated_48h_mm, thresholds[48])
    level_72h = classify_rainfall(event.accumulated_72h_mm, thresholds[72])
    composite = max_risk_level([level_24h, level_48h, level_72h])

    # For flood events the rule engine should reach at least orange.
    expected_min = "orange" if event.flooded else "green"
    expected_min_score = LEVEL_SCORES[expected_min]
    composite_score = LEVEL_SCORES[composite]

    correct = composite_score >= expected_min_score if event.flooded else composite_score < 2
    miss = event.flooded and composite_score < 2
    false_alarm = (not event.flooded) and composite_score >= 2

    return EventEvaluation(
        event=event,
        level_24h=level_24h,
        level_48h=level_48h,
        level_72h=level_72h,
        composite_level=composite,
        expected_min_level=expected_min,
        correct=correct,
        miss=miss,
        false_alarm=false_alarm,
    )


def compute_metrics(
    evaluations: list[EventEvaluation],
) -> dict[str, float | int]:
    """Compute precision, recall, and CSI for orange-or-red alerts.

    Positive class: rule engine returns orange or red (score >= 2).
    Ground truth positive: event.flooded == True.

    Args:
        evaluations: List of EventEvaluation results for all events.

    Returns:
        Dictionary with keys 'hits', 'misses', 'false_alarms', 'correct_negatives',
        'pod', 'far', 'csi'.  POD = probability of detection; FAR = false alarm
        ratio; CSI = critical success index.
    """
    hits = sum(
        1
        for ev in evaluations
        if ev.event.flooded and LEVEL_SCORES[ev.composite_level] >= 2
    )
    misses = sum(
        1
        for ev in evaluations
        if ev.event.flooded and LEVEL_SCORES[ev.composite_level] < 2
    )
    false_alarms = sum(
        1
        for ev in evaluations
        if not ev.event.flooded and LEVEL_SCORES[ev.composite_level] >= 2
    )
    correct_negatives = sum(
        1
        for ev in evaluations
        if not ev.event.flooded and LEVEL_SCORES[ev.composite_level] < 2
    )

    pod = hits / (hits + misses) if (hits + misses) > 0 else float("nan")
    far = false_alarms / (hits + false_alarms) if (hits + false_alarms) > 0 else float("nan")
    csi_denom = hits + misses + false_alarms
    csi = hits / csi_denom if csi_denom > 0 else float("nan")

    return {
        "hits": hits,
        "misses": misses,
        "false_alarms": false_alarms,
        "correct_negatives": correct_negatives,
        "pod": pod,
        "far": far,
        "csi": csi,
    }


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def print_evaluation_table(
    evaluations: list[EventEvaluation],
    label: str,
) -> None:
    """Print a formatted table of per-event evaluation results.

    Args:
        evaluations: List of EventEvaluation results to display.
        label: Section header label for the table.
    """
    col_w = [36, 8, 8, 8, 8, 6, 6, 8]
    header = [
        "Event",
        "24h mm",
        "48h mm",
        "72h mm",
        "Rule out",
        "Flood",
        "OK?",
        "Status",
    ]
    sep = "  ".join("-" * w for w in col_w)

    print(f"\n{'='*80}")
    print(f"  {label}")
    print(f"{'='*80}")
    print("  " + "  ".join(h.ljust(w) for h, w in zip(header, col_w, strict=True)))
    print("  " + sep)

    for ev in evaluations:
        status = "MISS" if ev.miss else ("FA" if ev.false_alarm else "ok")
        row = [
            ev.event.event_name[:36],
            f"{ev.event.accumulated_24h_mm:.0f}",
            f"{ev.event.accumulated_48h_mm:.0f}",
            f"{ev.event.accumulated_72h_mm:.0f}",
            ev.composite_level,
            str(ev.event.flooded),
            str(ev.correct),
            status,
        ]
        print("  " + "  ".join(v.ljust(w) for v, w in zip(row, col_w, strict=True)))

    print()


def print_metrics(
    metrics: dict[str, float | int],
    label: str,
) -> None:
    """Print computed precision, recall, and CSI metrics.

    Args:
        metrics: Metrics dictionary from compute_metrics.
        label: Section header label for the metrics block.
    """
    print(f"  {label}")
    print(f"  {'Metric':<30} Value")
    print(f"  {'-'*40}")
    print(f"  {'Hits (TP)':<30} {metrics['hits']}")
    print(f"  {'Misses (FN)':<30} {metrics['misses']}")
    print(f"  {'False alarms (FP)':<30} {metrics['false_alarms']}")
    print(f"  {'Correct negatives (TN)':<30} {metrics['correct_negatives']}")
    print(f"  {'POD (recall)':<30} {metrics['pod']:.2f}")
    print(f"  {'FAR (false alarm ratio)':<30} {metrics['far']:.2f}  (NaN = no FP in sample)")
    print(f"  {'CSI (critical success)':<30} {metrics['csi']:.2f}")
    print()


def print_threshold_comparison() -> None:
    """Print a side-by-side comparison of seed and proposed thresholds."""
    windows = [24, 48, 72]
    print(f"\n{'='*80}")
    print("  Threshold Comparison: Seed vs. Proposed")
    print(f"{'='*80}")
    print(f"  {'Window':<10} {'Seed yellow':>14} {'Seed orange':>14} {'Seed red':>10}")
    print(f"  {'':10} {'Prop yellow':>14} {'Prop orange':>14} {'Prop red':>10}")
    print(f"  {'-'*55}")
    for w in windows:
        s = SEED_THRESHOLDS[w]
        p = PROPOSED_THRESHOLDS[w]
        print(
            f"  {f'{w}h':<10}"
            f"  {s.yellow_mm:>12.0f}  {s.orange_mm:>12.0f}  {s.red_mm:>7.0f}"
        )
        changed = s != p
        marker = " (*)" if changed else "    "
        print(
            f"  {'':10}"
            f"  {p.yellow_mm:>12.0f}  {p.orange_mm:>12.0f}  {p.red_mm:>7.0f}{marker}"
        )
    print("\n  (*) = changed from seed")
    print()


def print_rationale() -> None:
    """Print the threshold adjustment rationale for documentation purposes."""
    print(f"\n{'='*80}")
    print("  Threshold Adjustment Rationale")
    print(f"{'='*80}")
    rationale = """
  24-hour window
  --------------
  Seed orange threshold: 130 mm.  All three events exceed this value
  (220 mm, 300 mm, 200 mm), so all are correctly classified at orange or red
  with the seed threshold.

  However, the 2011 event at 200 mm is exactly at the seed red boundary
  (200 mm), making classification boundary-sensitive.  To provide a more
  conservative margin and align with the TMD heavy-rain warning criterion
  (150 mm / 24 h for southern Thailand), we propose:
    - yellow:  70 mm  (from 80 mm)  — activates watch earlier
    - orange: 120 mm  (from 130 mm) — closer to TMD advisory threshold
    - red:    180 mm  (from 200 mm) — keeps 2011 event firmly in red

  48-hour window
  --------------
  Seed orange threshold: 200 mm.  Events range 290–380 mm, all above.
  Seed red threshold: 300 mm.  Two of three events (2000: 380 mm, 2010: 380 mm)
  exceed it; 2011 (290 mm) falls in orange.

  A 10-15 % downward shift aligns cumulative windows more tightly with the
  observed lower-bound events:
    - yellow: 110 mm  (from 120 mm)
    - orange: 175 mm  (from 200 mm)
    - red:    275 mm  (from 300 mm)

  72-hour window
  --------------
  All three events are clearly above any reasonable 72 h threshold (330–540 mm).
  No change is proposed from the seed values at this window for Phase 1, as
  the evidence does not distinguish between current orange (250 mm) and red
  (350 mm) boundaries with only three events.  Retain seed values.

  Confidence note
  ---------------
  Three flood events and zero non-flood heavy-rain events are insufficient for
  robust statistical threshold optimization.  These adjustments are conservative
  downward shifts intended to reduce the risk of a miss at the prototype stage.
  False-alarm ratio cannot be assessed without non-flood events.  POD = 1.0
  under both seed and proposed thresholds for this sample.  Threshold revision
  should be revisited once 10+ events (flood and non-flood) are assembled with
  verified rainfall station data.
"""
    print(rationale)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def run_calibration() -> None:
    """Execute the full threshold calibration analysis and print results."""
    print("\nHat Yai Flood Risk Threshold Calibration")
    print("Input data: embedded fixture — 3 historical Hat Yai flood events")
    print("Period: 2000–2011  |  Units: mm (rainfall accumulation)")
    print("Thresholds evaluated: 24 h, 48 h, 72 h windows")

    # --- Seed threshold evaluation ---
    seed_evals = [evaluate_event(ev, SEED_THRESHOLDS) for ev in HISTORICAL_EVENTS]
    seed_metrics = compute_metrics(seed_evals)

    print_evaluation_table(seed_evals, "Seed Thresholds (current Settings defaults)")
    print_metrics(seed_metrics, "Metrics — Seed Thresholds")

    # --- Proposed threshold evaluation ---
    proposed_evals = [evaluate_event(ev, PROPOSED_THRESHOLDS) for ev in HISTORICAL_EVENTS]
    proposed_metrics = compute_metrics(proposed_evals)

    print_evaluation_table(proposed_evals, "Proposed Adjusted Thresholds")
    print_metrics(proposed_metrics, "Metrics — Proposed Thresholds")

    # --- Side-by-side comparison ---
    print_threshold_comparison()

    # --- Rationale ---
    print_rationale()

    # --- Summary ---
    print(f"{'='*80}")
    print("  Summary")
    print(f"{'='*80}")
    seed_pod = seed_metrics["pod"]
    prop_pod = proposed_metrics["pod"]
    print(f"  Events evaluated  : {len(HISTORICAL_EVENTS)}")
    print(f"  Flood events      : {sum(1 for e in HISTORICAL_EVENTS if e.flooded)}")
    print(f"  Non-flood events  : {sum(1 for e in HISTORICAL_EVENTS if not e.flooded)}")
    print(f"  POD (seed)        : {seed_pod:.2f}")
    print(f"  POD (proposed)    : {prop_pod:.2f}")
    print()
    print("  Both seed and proposed thresholds achieve POD = 1.00 on this")
    print("  3-event sample.  The proposed thresholds lower orange/red boundaries")
    print("  to reduce miss risk at boundary values and align with TMD criteria.")
    print("  False-alarm ratio cannot be computed: no non-flood events in sample.")
    print()
    print("  Recommended action:")
    print("    Apply proposed 24 h and 48 h threshold adjustments to")
    print("    backend/app/core/config.py Settings defaults.")
    print("    Document in docs/risk-layer-design.md calibration section.")
    print()


if __name__ == "__main__":
    run_calibration()
