# Databricks notebook source
# MAGIC %md
# MAGIC # Rajasthan UDISE+ pipeline on Databricks (PySpark + Delta Lake)
# MAGIC
# MAGIC Bronze Parquet files (uploaded to a Unity Catalog volume) -> **silver** Delta tables (typed, cleaned,
# MAGIC rule-checked; failing rows in `quarantine_*` tables) -> **gold** Delta tables (star schema with an SCD
# MAGIC Type 2 school dimension, facts, district marts).
# MAGIC
# MAGIC The transforms are the functions in `spark/silver.py` and `spark/gold.py` of this repository (the same
# MAGIC ones the local tests run); this notebook only reads, writes and checks. Before anything is reported the
# MAGIC notebook runs the 33 data tests (port of `sql/tests/`) and the official 2024-25 totals check, and stops
# MAGIC on any failure. Finally it writes a small parity fingerprint (district-level aggregates only) to the
# MAGIC volume, to compare with the local DuckDB pipeline: `python -m spark.parity --from spark_fingerprint.json`.
# MAGIC
# MAGIC Setup steps: README, section "Running on Databricks". Source: UDISE+, Ministry of Education, Government
# MAGIC of India. School-level tables stay in this private workspace and must not be shared.

# COMMAND ----------

dbutils.widgets.text("catalog", "workspace", "Catalog")
dbutils.widgets.text("bronze_path", "/Volumes/workspace/udise/landing/bronze", "Bronze folder (volume)")
dbutils.widgets.text("parity_path", "/Volumes/workspace/udise/landing/parity", "Parity output folder (volume)")
dbutils.widgets.text("silver_schema", "udise_silver", "Silver schema")
dbutils.widgets.text("gold_schema", "udise_gold", "Gold schema")
# "no" only when running on the synthetic sample, which cannot match the published totals
dbutils.widgets.dropdown("check_official", "yes", ["yes", "no"], "Check official 2024-25 totals")

catalog = dbutils.widgets.get("catalog")
bronze_path = dbutils.widgets.get("bronze_path")
parity_path = dbutils.widgets.get("parity_path")
silver_schema = f"{catalog}.{dbutils.widgets.get('silver_schema')}"
gold_schema = f"{catalog}.{dbutils.widgets.get('gold_schema')}"

# COMMAND ----------

# MAGIC %md
# MAGIC ## Import the transforms from this Git folder
# MAGIC The notebook runs from `<repo>/spark/`, so the repository root is one level up. Import with
# MAGIC `from spark.x import y`: a plain `import spark` would replace this notebook's `spark` session.

# COMMAND ----------

import json
import os
import sys
import time
from datetime import datetime, timezone

repo_root = os.path.abspath(os.path.join(os.getcwd(), ".."))
if repo_root not in sys.path:
    sys.path.insert(0, repo_root)

from spark.checks import check_official, official_indicators, run_data_tests  # noqa: E402
from spark.gold import build_gold  # noqa: E402
from spark.parity import fingerprint_queries, spark_fingerprint, to_json, validation_sql  # noqa: E402
from spark.silver import build_silver  # noqa: E402

run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
print(f"Repository: {repo_root}\nRun id: {run_id}\nSpark {spark.version}")
for dataset in ["profile1", "facility", "teacher", "enrolment1"]:
    print(f"bronze/{dataset}: {len(dbutils.fs.ls(f'{bronze_path}/{dataset}'))} entries (files or year= folders)")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Where each table is written
# MAGIC `persist(name, df)` is called by the transforms for every table: it writes a Delta table (one new
# MAGIC version per run, tagged with the run id) and returns the table read back, so later steps read the
# MAGIC stored result instead of recomputing it. Silver tables are partitioned by year, like `data/silver/`.

# COMMAND ----------

spark.sql(f"create schema if not exists {silver_schema}")
spark.sql(f"create schema if not exists {gold_schema}")
written = {}


def table_name(name: str) -> str:
    if name.startswith("silver_"):
        return f"{silver_schema}.{name.removeprefix('silver_')}"
    if name.startswith("quarantine_"):
        return f"{silver_schema}.{name}"
    return f"{gold_schema}.{name}"


def persist(name, df):
    full = table_name(name)
    writer = (df.write.format("delta").mode("overwrite")
              .option("overwriteSchema", "true").option("userMetadata", run_id))
    if name.startswith("silver_"):
        writer = writer.partitionBy("year")
    t0 = time.perf_counter()
    writer.saveAsTable(full)
    stored = spark.table(full)
    written[full] = {"rows": stored.count(), "seconds": round(time.perf_counter() - t0, 1)}
    print(f"{full}: {written[full]['rows']:,} rows ({written[full]['seconds']} s)")
    return stored

# COMMAND ----------

# MAGIC %md
# MAGIC ## Bronze -> silver (+ quarantine)

# COMMAND ----------

timings = {}
t0 = time.perf_counter()
s = build_silver(spark, bronze_path, persist=persist, run_id=run_id)
timings["spark: bronze -> silver"] = round(time.perf_counter() - t0, 1)
for table, summary in s["summary"]["tables"].items():
    rules = {r: n for r, n in summary["quarantined_by_rule"].items() if n}
    print(f"{table}: {summary['rows_silver']:,} silver, {summary['rows_quarantined']:,} quarantined {rules}")
print("bronze = silver + quarantine:", s["summary"]["reconciled"])

# COMMAND ----------

# MAGIC %md
# MAGIC ## Silver -> gold (star schema, SCD2 dim_school, marts)

# COMMAND ----------

t0 = time.perf_counter()
gold = build_gold(spark, s["silver"], persist=persist)
timings["spark: silver -> gold"] = round(time.perf_counter() - t0, 1)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Data tests and official totals (stop on any failure)

# COMMAND ----------

tests = run_data_tests(spark, s["silver"], gold)
failed = [t for t in tests if t["failures"]]
print(f"{len(tests)} data tests, {len(failed)} failed")
official = check_official(official_indicators(spark, gold)) if dbutils.widgets.get("check_official") == "yes" else []
for o in official:
    print(f"{o['year']} {o['indicator']}: official {o['official']:,}, ours {o['ours']}, "
          f"{'exact match' if o['match'] else 'DIFFERS'}")
if failed or not all(o["match"] for o in official):
    raise RuntimeError(f"Checks failed: {failed} {[o for o in official if not o['match']]}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Parity fingerprint for the local DuckDB comparison
# MAGIC Download `spark_fingerprint.json` from the volume (Catalog Explorer, or
# MAGIC `databricks fs cp dbfs:<parity_path>/spark_fingerprint.json .`) and run
# MAGIC `python -m spark.parity --from spark_fingerprint.json` on your machine.

# COMMAND ----------

t0 = time.perf_counter()
fingerprint = spark_fingerprint(spark, s["silver"], s["quarantine"], gold, fingerprint_queries(validation_sql()))
timings["spark: fingerprint"] = round(time.perf_counter() - t0, 1)
os.makedirs(parity_path, exist_ok=True)
out = f"{parity_path}/spark_fingerprint.json"
with open(out, "w", encoding="utf-8") as fh:
    json.dump({"run_id": run_id, "spark_version": spark.version, "queries": to_json(fingerprint),
               "data_tests": tests, "timings_seconds": timings, "tables": written}, fh, indent=1)
print(f"Written {out}")
print(timings)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Look at the results
# MAGIC Every table is a Delta table: `DESCRIBE HISTORY` shows one version per run (with the run id in
# MAGIC `userMetadata`), and an earlier version can be read with `VERSION AS OF n` (time travel).

# COMMAND ----------

display(spark.table(f"{gold_schema}.mart_district_year").orderBy("district", "year"))

# COMMAND ----------

display(spark.sql(f"describe history {gold_schema}.dim_school")
        .select("version", "timestamp", "operation", "userMetadata"))
