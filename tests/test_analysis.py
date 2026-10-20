"""Analysis logic on made-up data (the real analysis needs the real gold database)."""
import numpy as np
import pandas as pd

from analysis.data import patterns
from analysis.models import SPECS, fit


def test_pattern_rules():
    dy = pd.DataFrame({
        "district": ["A", "B", "C", "D"],
        "girls_retention_gap": [-0.04, -0.05, 0.03, 0.0],
        "boys_progression": [0.6, 1.5, 0.7, 0.9],
        "st_share": [0.05, 0.03, 0.8, 0.8],
    })
    assert patterns(dy).tolist() == ["girls leaving", "boys moving in", "boys leaving (tribal south)", "other"]


def test_regression_recovers_a_planted_effect():
    """Simulate schools where a girls' toilet adds exactly 2 points; the model must find it."""
    rng = np.random.default_rng(0)
    n = 4000
    df = pd.DataFrame({
        "district": rng.choice([f"D{i}" for i in range(20)], n),
        "management_group": rng.choice(["State Government", "Private Unaided"], n),
        "location": rng.choice(["Rural", "Urban"], n),
        "school_category": rng.choice([3, 5], n),
        "has_girls_toilet": rng.integers(0, 2, n),
        "log_teachers": rng.normal(2, 0.5, n),
        "infra_other_10": rng.uniform(4, 10, n),
    })
    district_effect = df["district"].str[1:].astype(int) / 1000
    df["girls_share_sec"] = (0.45 + 0.02 * df["has_girls_toilet"] + district_effect
                             - 0.10 * (df["management_group"] == "Private Unaided")
                             + rng.normal(0, 0.05, n))
    model = fit(df, SPECS["main"])
    assert abs(model.params["has_girls_toilet"] - 0.02) < 0.005
    low, high = model.conf_int().loc["has_girls_toilet"]
    assert low < 0.02 < high
