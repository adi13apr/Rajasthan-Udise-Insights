"""Small Athena client used by aws/athena_build.py and aws/parity.py.

Every query runs in the workgroup udise-wg (1 GB scan limit per query, results to
athena-results/) against the Glue database udise. For each query the time and the bytes
Athena scanned are recorded (Athena bills by bytes scanned, with a 10 MB minimum per query).

CTAS ("CREATE TABLE ... AS SELECT") writes Parquet to an S3 prefix that must be empty, so a
rebuild first drops the table (metadata only) and deletes the files under its prefix. Only
the pipeline's own output prefixes may be cleared; bronze/ never.
"""
import time
from decimal import Decimal

import pandas as pd

from . import settings as S

CLEARABLE = ("silver/", "quarantine/rows/", "gold/", "reference/")


class AthenaError(RuntimeError):
    pass


class Athena:
    def __init__(self, profile: str = S.PROFILE, workgroup: str = S.WORKGROUP,
                 database: str = S.DATABASE, bucket: str = S.BUCKET):
        session = S.session(profile)
        self.athena = session.client("athena")
        self.s3 = session.client("s3")
        self.glue = session.client("glue")
        self.workgroup, self.database, self.bucket = workgroup, database, bucket
        self.log: list[dict] = []   # one entry per query: label, seconds, bytes scanned

    # --- Running queries ----------------------------------------------------------------------

    def execute(self, sql: str, label: str = "") -> str:
        """Run one statement and wait for it. Returns the query id; raises AthenaError on failure."""
        t0 = time.perf_counter()
        qid = self.athena.start_query_execution(
            QueryString=sql, WorkGroup=self.workgroup,
            QueryExecutionContext={"Database": self.database})["QueryExecutionId"]
        delay = 0.5
        while True:
            q = self.athena.get_query_execution(QueryExecutionId=qid)["QueryExecution"]
            state = q["Status"]["State"]
            if state in ("SUCCEEDED", "FAILED", "CANCELLED"):
                break
            time.sleep(delay)
            delay = min(delay * 1.5, 3.0)
        stats = q.get("Statistics", {})
        self.log.append({"label": label or sql.split("\n", 1)[0][:80], "state": state,
                         "seconds": round(time.perf_counter() - t0, 2),
                         "bytes_scanned": stats.get("DataScannedInBytes", 0), "query_id": qid})
        if state != "SUCCEEDED":
            reason = q["Status"].get("StateChangeReason", "")
            raise AthenaError(f"{label or 'query'} {state}: {reason}\n--- SQL (start) ---\n{sql[:1500]}")
        return qid

    def query_df(self, sql: str, label: str = "") -> pd.DataFrame:
        """Run a SELECT and return its result as a DataFrame with proper types."""
        qid = self.execute(sql, label)
        pages = self.athena.get_paginator("get_query_results").paginate(QueryExecutionId=qid)
        columns, rows = None, []
        for page in pages:
            rs = page["ResultSet"]
            if columns is None:
                columns = [(c["Name"], c["Type"]) for c in rs["ResultSetMetadata"]["ColumnInfo"]]
                data = rs["Rows"][1:]           # the first row of the first page is the header
            else:
                data = rs["Rows"]
            for r in data:
                rows.append([cell.get("VarCharValue") for cell in r["Data"]])
        df = pd.DataFrame(rows, columns=[n for n, _ in columns])
        for name, typ in columns:
            df[name] = df[name].map(lambda v, t=typ: convert(v, t))
        return df

    # --- Tables and their S3 data -------------------------------------------------------------

    def drop(self, name: str, view: bool = False) -> None:
        if view:
            self.execute(f"DROP VIEW IF EXISTS {name}", f"drop view {name}")
        else:
            self.execute(f"DROP TABLE IF EXISTS `{name}`", f"drop {name}")

    def clear_prefix(self, prefix: str) -> int:
        """Delete every object under s3://<bucket>/<prefix>. Only the pipeline's output prefixes."""
        if not prefix.startswith(CLEARABLE) or not prefix.endswith("/"):
            raise ValueError(f"refusing to clear s3://{self.bucket}/{prefix}")
        deleted = 0
        for page in self.s3.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=prefix):
            keys = [{"Key": o["Key"]} for o in page.get("Contents", [])]
            if keys:
                self.s3.delete_objects(Bucket=self.bucket, Delete={"Objects": keys, "Quiet": True})
                deleted += len(keys)
        return deleted

    def ctas(self, name: str, select_sql: str, prefix: str, partitioned_by: str | None = None) -> None:
        """(Re)create table `name` as Parquet under s3://<bucket>/<prefix> from a SELECT."""
        self.drop(name)
        self.clear_prefix(prefix)
        props = [f"format = 'PARQUET'", "write_compression = 'ZSTD'",
                 f"external_location = 's3://{self.bucket}/{prefix}'"]
        if partitioned_by:
            props.append(f"partitioned_by = ARRAY['{partitioned_by}']")
        self.execute(f"CREATE TABLE {name} WITH ({', '.join(props)}) AS\n{select_sql}", f"ctas {name}")

    def view(self, name: str, select_sql: str) -> None:
        self.execute(f"CREATE OR REPLACE VIEW {name} AS\n{select_sql}", f"view {name}")

    def columns(self, table: str) -> list[tuple[str, str]]:
        """(name, type) of a table's columns, partition columns last, from the Glue catalog."""
        t = self.glue.get_table(DatabaseName=self.database, Name=table)["Table"]
        cols = t["StorageDescriptor"]["Columns"] + t.get("PartitionKeys", [])
        return [(c["Name"], c["Type"]) for c in cols]

    def count(self, table: str) -> int:
        return int(self.query_df(f"SELECT count(*) AS n FROM {table}", f"count {table}")["n"][0])

    def summary(self) -> dict:
        return {"queries": len(self.log),
                "seconds": round(sum(q["seconds"] for q in self.log), 1),
                "bytes_scanned": sum(q["bytes_scanned"] for q in self.log)}


def convert(value, athena_type: str):
    """Athena returns every cell as text; turn it back into a Python value."""
    if value is None:
        return None
    t = athena_type.lower()
    if t in ("bigint", "integer", "smallint", "tinyint"):
        return int(value)
    if t in ("double", "float", "real"):
        return float(value)
    if t.startswith("decimal"):
        return Decimal(value)
    if t == "boolean":
        return value.lower() == "true"
    return value
