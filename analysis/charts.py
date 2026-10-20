"""Static charts (PNG) for the notebook and the policy brief.

Palette: the validated reference palette of the dataviz method (light mode).
Categorical slots used: blue, orange, aqua (validated all-pairs for scatter);
gray for context. Text always in ink colours, never in a series colour.
"""
import textwrap
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402

from pipeline import config as C  # noqa: E402

FIG_DIR = C.REPORTS_DIR / "figures"
SURFACE, INK, INK2, MUTED = "#fcfcfb", "#0b0b0b", "#52514e", "#898781"
GRID, AXIS, CONTEXT = "#e1e0d9", "#c3c2b7", "#d3d2cb"
BLUE, ORANGE, AQUA, RED = "#2a78d6", "#eb6834", "#1baf7a", "#e34948"
SOURCE = f"{C.SOURCE_NOTE} (UDISE+ 2024-25 and 2025-26 school-level data)."

plt.rcParams.update({
    "font.family": ["Segoe UI", "DejaVu Sans"], "font.size": 9, "text.color": INK,
    "axes.edgecolor": AXIS, "axes.labelcolor": INK2, "axes.facecolor": SURFACE,
    "figure.facecolor": SURFACE, "xtick.color": MUTED, "ytick.color": INK2,
    "axes.grid": False, "savefig.facecolor": SURFACE,
})


def _axes(height: float, width: float = 7.2):
    fig, ax = plt.subplots(figsize=(width, height), dpi=200)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    ax.tick_params(length=0)
    return fig, ax


def _finish(fig, title: str, subtitle: str, path: Path, note: str = "", label_above: bool = False) -> Path:
    """Title, subtitle and source note placed in inches, so spacing is the same on every chart."""
    h = fig.get_figheight()
    sub = textwrap.fill(subtitle, 118)
    footer = textwrap.fill((note + " " if note else "") + SOURCE, 150)
    top_in = 0.52 + 0.16 * sub.count("\n") + (0.18 if label_above else 0)
    bottom_in = 0.12 + 0.12 * (footer.count("\n") + 1)
    fig.text(0.01, 1 - 0.08 / h, title, ha="left", va="top", fontsize=12, fontweight="semibold")
    fig.text(0.01, 1 - 0.32 / h, sub, ha="left", va="top", fontsize=9, color=INK2, linespacing=1.3)
    fig.text(0.01, 0.06 / h, footer, ha="left", va="bottom", fontsize=7, color=MUTED)
    fig.tight_layout(rect=(0, bottom_in / h, 1, 1 - top_in / h))
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=200)
    plt.close(fig)
    return path


def ranking(df: pd.DataFrame, value: str, title: str, subtitle: str, path: Path, highlight: list[str],
            state_value: float, fmt: str = "{:.1f}", state_label: str = "State") -> Path:
    """All districts as sorted horizontal bars; the highlighted ones in blue, with values."""
    d = df.sort_values(value)
    fig, ax = _axes(8.6)
    colors = [BLUE if x in highlight else CONTEXT for x in d["district"]]
    ax.barh(d["district"].str.title(), d[value], height=0.62, color=colors)
    ax.axvline(state_value, color=INK2, lw=1, zorder=1)
    for y, (name, v) in enumerate(zip(d["district"], d[value])):
        if name in highlight:
            ax.text(v, y, " " + fmt.format(v), va="center", fontsize=8, color=INK, zorder=3,
                    bbox=dict(facecolor=SURFACE, edgecolor="none", pad=0.6))
    ax.text(state_value, len(d) - 0.2, f" {state_label} {fmt.format(state_value)}", fontsize=8,
            color=INK2, va="bottom")
    ax.xaxis.grid(True, color=GRID, lw=0.8)
    ax.set_axisbelow(True)
    ax.set_ylim(-0.7, len(d) - 0.1)
    ax.tick_params(axis="y", labelsize=7.5)
    return _finish(fig, title, subtitle, path, label_above=True)


def progression_scatter(df: pd.DataFrame, path: Path, height: float = 5.6) -> Path:
    """Boys' vs girls' progression ratio by district; the line marks equal progression."""
    groups = {"girls leaving": ORANGE, "boys moving in": BLUE, "boys leaving (tribal south)": AQUA}
    fig, ax = _axes(height)
    lo, hi = 0.45, 1.55
    ax.plot([lo, hi], [lo, hi], color=AXIS, lw=1, zorder=1)
    ax.text(1.13, 1.16, "equal for girls and boys", rotation=33, color=MUTED, fontsize=7.5)
    other = df[df["pattern"] == "other"]
    ax.scatter(other["boys_progression"], other["girls_progression"], s=34, color=CONTEXT,
               edgecolor=SURFACE, linewidth=1.5, zorder=2, label="other districts")
    for g, color in groups.items():
        sub = df[df["pattern"] == g]
        ax.scatter(sub["boys_progression"], sub["girls_progression"], s=46, color=color,
                   edgecolor=SURFACE, linewidth=1.5, zorder=3, label=g)
        for _, r in sub.iterrows():
            left = r["district"] in ("PRATAPGARH", "UDAIPUR")  # crowded: label on the left
            ax.annotate(r["district"].title(), (r["boys_progression"], r["girls_progression"]),
                        xytext=(-6 if left else 6, -3), textcoords="offset points", fontsize=7.5,
                        color=INK, ha="right" if left else "left")
    ax.set_xlim(lo, hi), ax.set_ylim(lo, 1.3)
    ax.set_xlabel("Boys: students per grade in classes 9-12 / per grade in classes 1-5")
    ax.set_ylabel("Girls: same ratio")
    ax.grid(True, color=GRID, lw=0.8)
    ax.set_axisbelow(True)
    ax.legend(frameon=False, fontsize=8, loc="upper left")
    return _finish(fig, "Three different reasons for a gender gap in secondary school",
                   "Progression ratio by district, 2025-26. Below the line: girls' enrolment falls off more "
                   "than boys' after class 8.", path)


def sector_dots(df: pd.DataFrame, districts: list[str], path: Path) -> Path:
    """Girls' retention gap in government vs non-government schools, for selected districts."""
    d = df[df["district"].isin(districts)].copy()
    order = (d[d["sector"] == "Government"].sort_values("girls_retention_gap")["district"].tolist())
    fig, ax = _axes(4.4)
    y = {name: i for i, name in enumerate(order)}
    for name in order:
        pair = d[d["district"] == name].set_index("sector")["girls_retention_gap"] * 100
        ax.plot([pair.min(), pair.max()], [y[name]] * 2, color=AXIS, lw=2, zorder=1)
    for sector, color in (("Government", BLUE), ("Non-government", ORANGE)):
        s = d[d["sector"] == sector]
        ax.scatter(s["girls_retention_gap"] * 100, s["district"].map(y), s=48, color=color,
                   edgecolor=SURFACE, linewidth=1.5, zorder=3, label=sector + " schools")
    ax.axvline(0, color=INK2, lw=1)
    ax.set_yticks(range(len(order)), [n.title() for n in order])
    ax.set_xlabel("Girls' share in classes 9-12 minus girls' share in classes 1-5 (percentage points)")
    ax.xaxis.grid(True, color=GRID, lw=0.8)
    ax.set_axisbelow(True)
    ax.legend(frameon=False, fontsize=8, loc="upper left")
    return _finish(fig, "Girls are a smaller share of senior students in non-government schools",
                   "Girls' retention gap by school sector, 2025-26, in the districts of the three patterns.", path)


def coefficients(table: pd.DataFrame, facts: dict, path: Path) -> Path:
    """Main-spec coefficients with 95% confidence intervals (percentage points)."""
    t = table[table["spec"] == "main"].iloc[::-1].reset_index(drop=True)
    fig, ax = _axes(3.6)
    ax.axvline(0, color=INK2, lw=1)
    for i, r in t.iterrows():
        ax.plot([r["ci_low_pp"], r["ci_high_pp"]], [i, i], color=BLUE, lw=2, solid_capstyle="round")
        ax.scatter(r["coef_pp"], i, s=46, color=BLUE, edgecolor=SURFACE, linewidth=1.5, zorder=3)
        ax.text(r["ci_high_pp"] + 0.35, i, f"{r['coef_pp']:+.1f}", va="center", fontsize=8, color=INK,
                bbox=dict(facecolor=SURFACE, edgecolor="none", pad=0.6))
    ax.set_yticks(range(len(t)), t["term"])
    ax.set_xlabel("Difference in girls' share of students in classes 9-12 (percentage points), 95% CI")
    ax.xaxis.grid(True, color=GRID, lw=0.8)
    ax.set_axisbelow(True)
    n = facts["main"]["n_schools"]
    return _finish(fig, "Schools with a working girls' toilet have a higher share of girls in classes 9-12",
                   f"Co-educational schools with 20+ students in classes 9-12, 2025-26 (n = {n:,}), "
                   "compared within the same district, location and management.", path,
                   note="Association, not cause. OLS with district fixed effects, errors clustered by district.")


def change_bars(df: pd.DataFrame, highlight: list[str], state_change: float, path: Path,
                top: int | None = None) -> Path:
    """Change in % single-teacher schools among matched schools, by district (all, or the top n)."""
    d = df.sort_values("change_pp")
    total_schools = int(d["schools_2025"].sum())
    if top:
        d = d.tail(top)
    fig, ax = _axes(8.6 if not top else 1.4 + 0.2 * top)
    colors = [ORANGE if x in highlight else CONTEXT for x in d["district"]]
    ax.barh(d["district"].str.title(), d["change_pp"], height=0.62, color=colors)
    for y, (name, v) in enumerate(zip(d["district"], d["change_pp"])):
        if name in highlight:
            ax.text(v, y, f" +{v:.1f}", va="center", fontsize=8, color=INK)
    ax.axvline(0, color=INK2, lw=1)
    ax.axvline(state_change, color=MUTED, lw=1)
    ax.text(state_change, len(d) - 0.2, f" State +{state_change:.1f}", fontsize=8, color=INK2, va="bottom")
    ax.xaxis.grid(True, color=GRID, lw=0.8)
    ax.set_axisbelow(True)
    ax.set_ylim(-0.7, len(d) - 0.1)
    ax.tick_params(axis="y", labelsize=7.5)
    ax.set_xlabel("Change in single-teacher schools, 2024-25 to 2025-26 (percentage points)")
    return _finish(fig, "Single-teacher schools rose fastest in the western desert",
                   f"Same schools in both years ({total_schools:,}), each counted in its "
                   "2025-26 district. Western desert districts in orange."
                   + (f" The {top} of 41 districts with the largest rise." if top else ""),
                   path, label_above=True)
