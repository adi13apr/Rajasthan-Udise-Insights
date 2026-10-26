"""PySpark implementation of the bronze -> silver -> gold transforms (runs locally or on Databricks).

The DuckDB pipeline (pipeline/, sql/) stays the reference. This package rebuilds the same
silver tables (with the same quarantine rules) and the same gold star schema and marts in
Spark, and spark/parity.py checks that both engines give the same answers.

    spark/session.py              local SparkSession for tests and the parity check
    spark/silver.py               bronze Parquet -> silver tables + quarantine (rules from pipeline/rules.py)
    spark/gold.py                 silver -> dimensions, SCD2 dim_school, facts, int_school_year, marts
    spark/checks.py               the sql/tests data tests and the official indicators, in Spark
    spark/parity.py               DuckDB gold vs Spark gold (python -m spark.parity)
    spark/databricks_pipeline.py  Databricks notebook: Unity Catalog volume -> Delta tables

Only the DataFrame API and Spark SQL are used (no RDDs, no sparkContext, no .cache() inside
the transforms), so the same code runs on Databricks serverless (Spark Connect).
Note for notebooks: import with `from spark.silver import ...`, never `import spark`, which
would replace the notebook's `spark` session variable.
"""
