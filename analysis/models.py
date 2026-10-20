"""A3: school-level regression of girls' share at secondary on facilities and teachers."""
import pandas as pd
import statsmodels.formula.api as smf

TERMS = {
    "has_girls_toilet": "Functional girls' toilet (yes vs no)",
    "log_teachers": "Teachers (log; +1 = about 2.7x as many)",
    "infra_other_10": "Other facilities index (+10 points)",
    "C(location)[T.Urban]": "Urban (vs rural)",
    "C(management_group, Treatment('State Government'))[T.Private Unaided]": "Private unaided (vs state govt)",
}
CONTROLS = "C(location) + C(management_group, Treatment('State Government')) + C(district)"
SPECS = {
    "main": f"girls_share_sec ~ has_girls_toilet + log_teachers + infra_other_10 + {CONTROLS}",
    "with school category": (f"girls_share_sec ~ has_girls_toilet + log_teachers + infra_other_10 + "
                             f"{CONTROLS} + C(school_category)"),
}


def fit(df: pd.DataFrame, formula: str):
    """OLS with district fixed effects; standard errors clustered by district (41 clusters),
    because schools in the same district share unobserved conditions."""
    return smf.ols(formula, data=df).fit(cov_type="cluster", cov_kwds={"groups": df["district"]})


def tidy(model, spec: str) -> pd.DataFrame:
    ci = model.conf_int()
    rows = [{"spec": spec, "term": label, "coef_pp": model.params[t] * 100,
             "ci_low_pp": ci.loc[t, 0] * 100, "ci_high_pp": ci.loc[t, 1] * 100, "p_value": model.pvalues[t]}
            for t, label in TERMS.items() if t in model.params]
    return pd.DataFrame(rows)


def run(df: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """Fit every spec. Returns the coefficient table (percentage points) and model facts."""
    tables, facts = [], {}
    for spec, formula in SPECS.items():
        m = fit(df, formula)
        tables.append(tidy(m, spec))
        facts[spec] = {"n_schools": int(m.nobs), "r_squared": round(m.rsquared, 3),
                       "clusters": int(df["district"].nunique())}
    return pd.concat(tables, ignore_index=True), facts
