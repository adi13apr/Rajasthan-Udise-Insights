"""Bronze layer: a faithful Parquet copy of every raw zip (ELT: load first, transform later).

For each raw zip we write one Parquet file with all the original columns,
values kept as text exactly as in the CSV (no cleaning, no type guessing), plus
load metadata:

    _source_file    zip file name the row came from
    _source_row     row number inside that file (1 = first data row), for lineage
    _district_file  district parsed from the file name (RAJASTHAN = state-wide file)
    _year           academic year parsed from the file name
    _ingested_at    when this file was loaded (UTC)
    _run_id         pipeline run that loaded it

Layout (Hive-style partitions, so DuckDB can skip whole years):

    data/bronze/<dataset>/year=<YYYY-YY>/<source_file_stem>.parquet

Bronze keeps duplicates (e.g. Kota schools that are in both the Kota file and
the state-wide file); removing them is a silver decision.

Incremental: the SHA-256 checksum of each zip is stored in data/bronze/_state.json.
A zip whose checksum has not changed since the last run (and whose Parquet file
still exists) is skipped.
"""
import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from .config import BRONZE_DIR, BRONZE_STATE, RAW_DIR
from .ingest import RawFile, discover, read_zip_csv

# Bump when the bronze file format changes (columns, types); forces a full reload.
BRONZE_FORMAT = 1


def new_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def bronze_path(rf: RawFile, bronze_dir: Path = BRONZE_DIR) -> Path:
    return bronze_dir / rf.dataset / f"year={rf.year}" / f"{rf.path.stem}.parquet"


def read_state(state_path: Path) -> dict:
    if state_path.exists():
        state = json.loads(state_path.read_text(encoding="utf-8"))
        if state.get("format") == BRONZE_FORMAT:
            return state
    return {"format": BRONZE_FORMAT, "files": {}}


def write_state(state: dict, state_path: Path) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = state_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(state_path)


def to_bronze(rf: RawFile, run_id: str, ingested_at: pd.Timestamp) -> pd.DataFrame:
    """Raw CSV as text + load metadata columns. No cleaning."""
    df = read_zip_csv(rf.path, as_text=True)
    df["_source_file"] = rf.path.name
    df["_source_row"] = range(1, len(df) + 1)
    df["_district_file"] = rf.district
    df["_year"] = rf.year
    df["_ingested_at"] = ingested_at
    df["_run_id"] = run_id
    return df


def load_bronze(raw_dir: Path = RAW_DIR, bronze_dir: Path = BRONZE_DIR,
                run_id: str | None = None, full_refresh: bool = False) -> dict:
    """Copy every raw zip into bronze Parquet; skip zips that have not changed.

    Returns a summary for the run manifest: one entry per source file with its
    status (loaded / skipped), row and column counts, checksum and timing.
    """
    run_id = run_id or new_run_id()
    state_path = bronze_dir / BRONZE_STATE.name
    state = {"format": BRONZE_FORMAT, "files": {}} if full_refresh else read_state(state_path)
    files, warnings = discover(raw_dir)
    ingested_at = pd.Timestamp.now(tz="UTC")

    entries = []
    for rf in files:
        key = rf.path.relative_to(raw_dir).as_posix()
        out = bronze_path(rf, bronze_dir)
        checksum = file_sha256(rf.path)
        prev = state["files"].get(key)
        if prev and prev["sha256"] == checksum and out.exists():
            entries.append({**prev, "source": key, "status": "skipped", "seconds": 0.0})
            continue

        t0 = time.perf_counter()
        df = to_bronze(rf, run_id, ingested_at)
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_suffix(".parquet.tmp")
        df.to_parquet(tmp, index=False, compression="zstd")
        tmp.replace(out)  # atomic: a crash never leaves a half-written file behind

        record = {
            "dataset": rf.dataset,
            "year": rf.year,
            "district_file": rf.district,
            "sha256": checksum,
            "rows": len(df),
            "columns": df.shape[1] - 6,  # original columns, without the metadata
            "bronze_file": out.relative_to(bronze_dir).as_posix(),
            "bronze_bytes": out.stat().st_size,
            "run_id": run_id,
        }
        state["files"][key] = record
        write_state(state, state_path)  # after every file, so an interrupted run resumes
        entries.append({**record, "source": key, "status": "loaded",
                        "seconds": round(time.perf_counter() - t0, 3)})

    current = {e["source"] for e in entries}
    for key, rec in state["files"].items():
        if key not in current:
            warnings.append(f"bronze: {rec['bronze_file']} has no source zip any more "
                            f"({key}); left in place, remove it by hand if intended")
    return {"run_id": run_id, "files": entries, "warnings": warnings}


def summarise_bronze(result: dict) -> dict:
    """Totals for the run manifest: files loaded/skipped, rows per dataset and year."""
    files = result["files"]
    rows: dict[str, dict[str, int]] = {}
    for e in files:
        rows.setdefault(e["dataset"], {}).setdefault(e["year"], 0)
        rows[e["dataset"]][e["year"]] += e["rows"]
    return {
        "files_total": len(files),
        "files_loaded": sum(e["status"] == "loaded" for e in files),
        "files_skipped": sum(e["status"] == "skipped" for e in files),
        "rows_by_dataset_year": rows,
        "bronze_bytes": sum(e["bronze_bytes"] for e in files),
    }
