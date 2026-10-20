"""Build every analysis output from gold: figures, district-level tables and the numbers
the brief quotes.

    python -m analysis.run
"""
import json

from pipeline import config as C

from . import charts as Ch
from . import data as D
from . import exports as E
from . import models as M

TABLE_DIR = C.REPORTS_DIR / "analysis"


def build() -> dict:
    TABLE_DIR.mkdir(parents=True, exist_ok=True)
    dy = D.district_year(D.LATEST)
    dy["pattern"] = D.patterns(dy)
    state = D.state_totals(D.LATEST)

    # A1: district rankings
    bottom_infra = dy.nsmallest(10, "avg_infra_index")["district"].tolist()
    top_single = dy.nlargest(10, "pct_single_teacher")["district"].tolist()
    top_ptr = dy.nlargest(10, "ptr")["district"].tolist()
    Ch.ranking(dy, "avg_infra_index", "The tribal south has the weakest school infrastructure",
               "Average infrastructure index (0-100: share of 8 basic facilities present), 2025-26. "
               "Bottom 10 in blue.", Ch.FIG_DIR / "a1_infrastructure_index.png", bottom_infra,
               state["avg_infra_index"])
    Ch.ranking(dy, "pct_single_teacher", "One school in five has a single teacher in Pratapgarh",
               "Share of schools with exactly one teacher (%), 2025-26. Top 10 in blue.",
               Ch.FIG_DIR / "a1_single_teacher.png", top_single, state["pct_single_teacher"])
    Ch.ranking(dy, "ptr", "Barmer and Jalore have the most students per teacher",
               "Students in classes 1-12 per teacher, 2025-26. Top 10 in blue.",
               Ch.FIG_DIR / "a1_ptr.png", top_ptr, state["ptr"])
    rankings = dy[["district", "schools", "enrolment", "avg_infra_index", "pct_single_teacher", "ptr"]].copy()
    for col, ascending in (("avg_infra_index", True), ("pct_single_teacher", False), ("ptr", False)):
        rankings[f"rank_{col}"] = rankings[col].rank(ascending=ascending, method="min").astype(int)  # 1 = worst
    rankings.sort_values("avg_infra_index").to_csv(TABLE_DIR / "a1_district_rankings_2025-26.csv", index=False)

    # A2: girls' secondary enrolment
    Ch.progression_scatter(dy, Ch.FIG_DIR / "a2_progression.png")
    sector = D.girls_share_by_sector()
    pattern_districts = dy.loc[dy["pattern"] != "other", "district"].tolist()
    Ch.sector_dots(sector, pattern_districts, Ch.FIG_DIR / "a2_sector.png")
    dy[["district", "pattern", "girls_share_primary", "girls_share_sec_plus", "girls_retention_gap",
        "boys_progression", "girls_progression", "st_share"]].sort_values("girls_retention_gap").to_csv(
        TABLE_DIR / "a2_girls_secondary_2025-26.csv", index=False)
    sector.round(4).to_csv(TABLE_DIR / "a2_girls_share_by_sector_2025-26.csv", index=False)

    # A3: school-level regression (only coefficients are saved)
    table, facts = M.run(D.regression_frame())
    table.round(4).to_csv(TABLE_DIR / "a3_regression_coefficients.csv", index=False)
    Ch.coefficients(table, facts, Ch.FIG_DIR / "a3_coefficients.png")

    # A4: matched schools, year on year
    matched = D.matched_single_teacher()
    state_change = (matched["single_teacher_schools_2025"].sum() / matched["schools_2025"].sum()
                    - matched["single_teacher_schools_2024"].sum() / matched["schools_2024"].sum()) * 100
    matched.round(3).to_csv(TABLE_DIR / "a4_single_teacher_change_matched.csv", index=False)
    Ch.change_bars(matched, D.WESTERN_DESERT, state_change, Ch.FIG_DIR / "a4_single_teacher_change.png")

    # Compact versions for the two-page policy brief
    Ch.progression_scatter(dy, Ch.FIG_DIR / "brief_patterns.png", height=4.3)
    Ch.change_bars(matched, D.WESTERN_DESERT, state_change, Ch.FIG_DIR / "brief_desert.png", top=12)

    desert = matched[matched["district"].isin(D.WESTERN_DESERT)]
    numbers = {
        "state_2025_26": {k: round(float(v), 3) for k, v in state.items()},
        "patterns": dy.groupby("pattern")["district"].apply(list).to_dict(),
        "regression": {"facts": facts, "main": table[table["spec"] == "main"].round(3).to_dict("records")},
        "matched_single_teacher": {
            "schools": int(matched["schools_2025"].sum()), "state_change_pp": round(state_change, 3),
            "western_desert": desert[["district", "pct_single_teacher_2024", "pct_single_teacher_2025",
                                      "change_pp", "teachers_change_pct"]].round(2).to_dict("records")},
    }
    numbers["exports"] = E.build()  # A5: Tableau CSVs and the Excel workbook
    (TABLE_DIR / "numbers.json").write_text(json.dumps(numbers, indent=2, default=str), encoding="utf-8")
    return numbers


if __name__ == "__main__":
    print(json.dumps(build(), indent=1, default=str))
