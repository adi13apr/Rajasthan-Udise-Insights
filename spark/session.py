"""A local SparkSession (tests and the parity check). Databricks provides its own `spark`."""
import os
import shutil
import sys
from pathlib import Path


def java_available() -> bool:
    """Java is needed to start Spark: JAVA_HOME/bin/java or java on PATH."""
    home = os.environ.get("JAVA_HOME")
    if home and any((Path(home) / "bin" / exe).exists() for exe in ("java", "java.exe")):
        return True
    return shutil.which("java") is not None


def spark_available() -> bool:
    try:
        import pyspark  # noqa: F401
    except ImportError:
        return False
    return java_available()


def cache_and_count(name: str, df):
    """persist() for local runs: compute each table once and keep the rows in memory.

    localCheckpoint(), not cache(): a cached table keeps its whole lineage, so every later plan
    carries every earlier one (the mart plans grew to millions of characters and task
    serialisation ran out of memory). A checkpoint starts later plans from the stored rows,
    which is what writing and re-reading a Delta table does in the Databricks notebook
    (serverless does not allow either cache() or checkpoints)."""
    return df.localCheckpoint(eager=True)


def local_session(app: str = "udise", cores: int | str = "*", memory: str = "4g"):
    """Local Spark, set up like Databricks where it matters: UTC session time zone and ANSI
    SQL mode (on by default in Spark 4 and on Databricks serverless: a bad cast or a
    division by zero raises an error instead of returning NULL)."""
    from pyspark.sql import SparkSession

    # The Python workers must be this interpreter (matters on Windows, where `python`
    # on PATH may be a different install).
    os.environ.setdefault("PYSPARK_PYTHON", sys.executable)
    os.environ.setdefault("PYSPARK_DRIVER_PYTHON", sys.executable)
    return (SparkSession.builder
            .master(f"local[{cores}]")
            .appName(app)
            .config("spark.driver.memory", memory)
            .config("spark.sql.session.timeZone", "UTC")
            .config("spark.sql.ansi.enabled", "true")
            # Small data: a few shuffle partitions instead of the default 200.
            .config("spark.sql.shuffle.partitions", "8")
            .config("spark.ui.enabled", "false")
            .config("spark.sql.execution.arrow.pyspark.enabled", "true")
            .getOrCreate())
