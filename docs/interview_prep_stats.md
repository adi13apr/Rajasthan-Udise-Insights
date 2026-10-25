# Interview prep: statistical analysis & regression module

Short answers to the questions most likely to come up about `notebooks/statistical_analysis.ipynb`.
Numbers are from the run of 25 Oct 2026 (`reports/analysis/stats_module_results.json`), after the fixes
to reference levels, tuning grids and p-value display, and with a fixed row order (see
"Are your results reproducible?"). Two consecutive runs gave identical results.

## Data and sample

**Why one row per school, and why only 2025-26?**
The questions are about schools (staffing, facilities), so the school is the unit. One year avoids
counting the same school twice; year-on-year change is analysed separately on matched schools.

**Why did you exclude 644 schools?**
278 had no students in classes 1-12 and 366 had students but no teachers. For both, students per teacher
is undefined (division by zero). Imputing a value would invent data, so they are excluded from modelling
and counted in the anomaly table, not silently dropped.

## Missing values

**Why not fill missing values with the mean or median?**
None of the gaps is random, so a mean would hide information. Each gets a treatment based on why it is
missing:
- `has_handwash` (3,254 schools) is missing because UDISE+ uses code 9 = "not applicable / not reported".
  I filled 0 and added a `handwash_not_reported` flag, so the model can treat "not reported" separately
  instead of pretending it is "yes" or "no".
- The classroom-repair share (1,862 schools) is missing only where a school reports 0 classrooms (0/0).
  That is structurally missing; I set it to 0 and added a `no_classrooms` flag. The notebook asserts
  that every missing value is a 0-classroom school.
- `block` (114) is missing for central schools listed under an admin region; it became a category.

**What is a missing-indicator and why use it?**
A 0/1 column saying "this value was missing". It keeps the row and lets the model learn whether
missingness itself carries information (here it does: not-reported codes are not random).

## Outliers

**Why both IQR and z-score?**
IQR does not assume a normal distribution, which suits skewed counts. A z-score assumes a roughly normal
shape, so I applied it on the log scale. They disagree a lot on raw data: for students per teacher, 4,530
schools are IQR outliers but only 1,297 have |z| > 3 on the log scale. The skew makes IQR flag many
legitimate large values.

**How did you decide keep vs cap vs drop?**
- **Keep** genuine extremes (very large schools exist), modelled on a log scale so they have less leverage.
- **Cap** (winsorise at the 1st and 99th percentiles) engineered ratios such as students per classroom.
  A tiny denominator (one classroom) creates extreme values that are more likely reporting quirks and
  would dominate a linear model. The row stays; only the extreme value is pulled in.
- **Drop** only impossible values (zero denominators).

**Why cap features but not the target?**
Capping the target changes what you are predicting. The target (PTR) is log-transformed instead, which
reduces skew without altering any value's rank.

## Encoding and imbalanced categories

**Why one-hot encoding and not label encoding?**
Management, location and level have no numeric order. Label encoding (1, 2, 3) would tell a linear model
that "Private" is twice "State Government". One-hot gives each category its own coefficient.

**Why drop one dummy, and why the largest group?**
Keeping all dummies plus an intercept is perfect multicollinearity (the dummy variable trap). The dropped
level becomes the reference. I chose the largest group (State Government, Primary, Rural, the largest
district). In a first run, pandas' default dropped the alphabetically first group, a small one, and two
management dummies got VIFs near 200: with a tiny reference, the remaining dummies almost add up to 1.
With the largest group as reference, every VIF is below 5 (max 4.09).

**What did you do about rare categories?**
Central Government and Other Government had under 1% of schools each; they were merged into "Other" (122
schools), so no coefficient rests on a handful of schools. School type had 43 boys-only and 2,476
girls-only schools, so it became one Boolean, `co_ed`.

## Feature engineering

**Why ratios instead of raw counts?**
Raw counts mostly measure size. Students per teacher, students per classroom and teacher shares compare
a school of 30 with a school of 3,000 on conditions, not size.

**Which feature are you proudest of?**
`girls_toilets_per_100_girls`: it measures adequacy, not just presence. A school with one toilet for 400
girls "has a girls' toilet" but is not meeting the need.

**Why log(enrolment)?**
Enrolment is right-skewed. On the log scale, a coefficient reads as an elasticity: in the multiple model,
10% more students goes with 6.8% more students per teacher.

## Correlation

**Pearson or Spearman?**
Both. Pearson measures linear association and is sensitive to outliers; Spearman uses ranks and captures
any monotonic relationship. Students per classroom vs log(PTR): Pearson 0.617, Spearman 0.725. The gap
says the relationship is monotonic but not linear.

**Why Cramér's V after a chi-square test?**
With 100k schools the chi-square test is significant for almost anything. Cramér's V (0 to 1) measures
strength: location vs management is 0.445 (a strong association), single-teacher vs location only
0.046 (weak).

**Point-biserial vs ANOVA?**
Point-biserial is Pearson's r between a 0/1 variable and a number. ANOVA handles a category with several
levels; eta squared gives the share of variance explained (school level explains 28.8% of the variation
in the infrastructure index).

## Hypothesis tests

**Why Welch's t-test instead of Student's?**
Welch does not assume equal variances. Rural and urban, or government and private, groups have different
spreads and sizes; Welch is the safer default and costs almost nothing when variances are equal.

**Your p-values are tiny. Does that mean the effects are big?**
No. With ~100k schools, even trivial differences are significant. Rural vs urban PTR: p ~ 2e-30, but
Cohen's d = 0.10 (negligible; medians 14.5 vs 16.2). Girls' share in classes 9-12, government vs
non-government: 52.7% vs 40.3%, d = 0.95 (large). That is why every test reports an effect size.

**Why add a test at district level?**
To show a case where the sample is small (n = 41, only 5 tribal-majority districts) and significance is
not automatic. Tribal-majority districts average an infrastructure index of 68.3 vs 81.1, p = 9.4e-05,
d = -3.19: a very large, consistent gap that is significant even with 5 districts.

**Why Mann-Whitney and Kruskal-Wallis alongside the t-test and ANOVA?**
They make no normality assumption (they compare ranks). When both agree, the conclusion does not depend
on the distribution assumption.

**What does a p-value actually mean?**
If H0 were true, the probability of seeing a difference at least this large by chance. It is not the
probability that H0 is true, and it says nothing about the size of the effect.

## Linear regression

**Why log(PTR) as the target?**
PTR is right-skewed. On the log scale the residuals are closer to symmetric, and coefficients read as
percentage changes: exp(b) - 1.

**Why district fixed effects?**
District dummies compare each school only with schools in its own district. That absorbs district
staffing policy, geography and demand that I cannot measure directly.

**How do you read R² = 0.756?**
The model explains 75.6% of the variation in log(PTR) for schools it never saw (test set). Adjusted R² is
almost the same (0.755) because ~85k rows easily support ~60 predictors. Simple regression on size alone gave
0.523, so the other features add about 23 points.

**What does RMSE = 0.373 mean in plain terms?**
It is on the log scale; exp(0.373) = 1.45, so a typical prediction is off by a factor of about 1.45
(e.g. predicting 20 when the truth is 14 or 29).

**How did you check the assumptions?**
- Linearity and constant variance: residuals vs fitted plot. Breusch-Pagan p < 0.001 says the variance is
  not constant, so I report HC3 robust standard errors.
- Normality of residuals: Q-Q plot and histogram. Skew 0.55, kurtosis 6.01 (heavier tails than normal).
  With 85k residuals formal tests reject tiny departures; normality matters for p-values, not for the
  coefficients, and the robust errors and large n make inference reliable.
- Multicollinearity: VIF; `grade_span` was removed (VIF > 5), and all remaining VIFs are below 5.

**Interpret a coefficient.**
Higher-secondary schools have 57.7% fewer students per teacher than primary schools of the same size in
the same district (a likely reason, not tested here: senior classes need a teacher per subject). Urban schools have 4.6% more, private
unaided 8.1% fewer than state-government schools.

**Is the model causal?**
No. A crowded school may get more teachers because it is crowded (reverse causality), and unmeasured
factors (vacancies, distance) matter. The coefficients describe associations.

## Logistic regression and comparison

**Why predict single-teacher schools?**
It is actionable: a ranked list of schools at risk helps target teacher deployment.

**Why are no teacher features in that model?**
Teacher counts define the target. Using them (or anything derived from them, like PTR) would be target
leakage: the model would look perfect and be useless.

**How did you handle the class imbalance (6.7% positives)?**
Stratified train/test split (same share in both), `class_weight="balanced"`, and metrics that stay honest
under imbalance. A model that always says "no" is 93.3% accurate and useless, so accuracy is not reported.

**Why PR-AUC and not only ROC-AUC?**
ROC-AUC can look high with many true negatives. PR-AUC focuses on the positive class, and its baseline is
the positive rate: the random forest scores 0.616 against 0.067 for a random guess (ROC-AUC 0.927).

**Precision is only 22% at 92% recall. Is that bad?**
It is a trade-off set by the 0.5 threshold with balanced class weights. For a screening list, missing
few at-risk schools (high recall) matters more than some false alarms. Raising the threshold buys
precision at the cost of recall; a precision-recall curve would show every option (the notebook draws
ROC curves; adding a PR curve is a quick extension).

**Why did the random forest beat logistic regression?**
It captures non-linear effects and interactions (size matters differently by level and district).
Logistic regression is still useful: its odds ratios are easy to explain. Against rural schools (the larger
group), urban schools have about 2.2 times the odds of being single-teacher once size was held constant, even though the raw
rate is lower in urban schools (4.3% vs 7.3%). Rural schools are smaller on average (section 1.2) and size
is the strongest predictor, so size confounds the raw comparison.

**How were hyperparameters chosen?**
By 5-fold cross-validation on the training set only: `C` for logistic regression, `max_depth` and
`min_samples_leaf` for the tree. In earlier runs the best values sat on the edge of their grids (C = 0.01,
then the smallest tried; depth 12 and later leaf size 100, the largest), so the grids were widened. Now every
best value is inside its grid: C = 0.01 (0.001 to 10), depth 30 (3 to 40), leaf size 100 (20 to 800). A value
on the edge means the real optimum may lie beyond the range searched.

## Overfitting

**How do you know the models do not overfit?**
Train vs test gaps are small: linear regression R² 0.760 vs 0.756; random forest PR-AUC 0.606 vs 0.616
(slightly higher on test, which is noise, not a problem); tuned tree 0.601 vs 0.586. 5-fold CV R² for the
linear model is 0.760 ± 0.003, so the result is stable across folds.

**Show me overfitting.**
An unconstrained decision tree: PR-AUC 0.999 on training data and 0.238 on the test set. The depth curve
shows training scores rising with depth while validation scores fall after a point (at depth 18, 0.745
train vs 0.398 validation).

**What did you do about it?**
Limited tree depth and leaf size (chosen by CV), averaged 300 trees in the forest, regularised the
logistic regression, and used the test set only once, at the end.

## Reproducibility

**Are your results reproducible?**
Yes, and checking it found a bug. The seed was fixed, yet two runs gave slightly different numbers (test R²
0.755 vs 0.754). The cause: DuckDB runs the join in parallel and returns rows in any order, so
`train_test_split(random_state=42)` drew a different set of schools each time. The query now ends with
`order by` the school code, and two consecutive runs give byte-identical results. A seed only fixes the
shuffle; the input order has to be fixed too.

## Big picture

**What would you do next?**
Validate the single-teacher model on next year's data (temporal validation), add teacher vacancy and
distance data if available, and test a policy change (e.g. girls' toilets) with a pilot rather than
reading it off a regression.

**How would you explain the findings to a non-technical teammate?**
Bigger schools have more students per teacher, and among schools of the same size, primary schools have
far more than senior ones. Rural and urban schools differ only slightly in staffing. For girls in senior
classes, the big difference is between government and private schools (52.7% vs 40.3% girls). And we
can list most schools at risk of having a single teacher: about one in five flagged schools truly is.
