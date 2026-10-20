"""Find raw UDISE+ zip files and load them into one table per dataset.

Raw zips are read in place (never extracted or modified). District and year
are taken from the file name, so new districts or years can be dropped into
data/raw/<year>/ without any code changes.
"""
import zipfile
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from .config import COLUMN_RENAMES, DATASETS, RAW_DIR


@dataclass
class RawFile:
    path: Path
    dataset: str
    district: str
    year: str


def parse_name(path: Path) -> RawFile | None:
    """Split e.g. 'profile_data_1_KOTA_2024-25.zip' into its parts."""
    stem = path.stem
    for prefix, key in DATASETS.items():
        if stem.startswith(prefix + "_"):
            rest = stem[len(prefix) + 1:]
            district, _, year = rest.rpartition("_")
            if district and year:
                return RawFile(path, key, district.strip().upper(), year)
    return None


def discover(raw_dir: Path = RAW_DIR) -> tuple[list[RawFile], list[str]]:
    """Return parsed raw files plus warnings about anything unexpected."""
    files, warnings = [], []
    for path in sorted(raw_dir.rglob("*.zip")):
        rf = parse_name(path)
        if rf is None:
            warnings.append(f"Unrecognised file name, skipped: {path.name}")
            continue
        if path.parent.name != rf.year:
            warnings.append(
                f"{path.name} is in folder '{path.parent.name}' but its name says {rf.year}"
            )
        files.append(rf)
    return files, warnings


def read_zip_csv(path: Path) -> pd.DataFrame:
    with zipfile.ZipFile(path) as z:
        csvs = [n for n in z.namelist() if n.lower().endswith(".csv")]
        if len(csvs) != 1:
            raise ValueError(f"{path.name}: expected 1 CSV inside, found {csvs}")
        with z.open(csvs[0]) as fh:
            df = pd.read_csv(fh, low_memory=False)
    df.columns = [c.strip().lower() for c in df.columns]
    return df.rename(columns=COLUMN_RENAMES)


def load_all(raw_dir: Path = RAW_DIR) -> tuple[dict[str, pd.DataFrame], list[str]]:
    """Load every dataset, stacked across districts and years.

    Columns that exist in only some years are kept and left empty for the
    others, so schema changes between years never break the pipeline.
    """
    files, warnings = discover(raw_dir)
    frames: dict[str, list[pd.DataFrame]] = {}
    for rf in files:
        df = read_zip_csv(rf.path)
        df.insert(0, "year", rf.year)
        df.insert(1, "district_file", rf.district)
        frames.setdefault(rf.dataset, []).append(df)
    tables = {}
    for k, v in frames.items():
        df = pd.concat(v, ignore_index=True)
        # The same school can arrive twice (e.g. a single-district file plus the
        # whole-state "All" file). Keep one copy and report it.
        key = ["year", "pseudocode"] + (["item_group", "item_id"] if k.startswith("enrolment") else [])
        dup = df.duplicated(key)
        if dup.any():
            warnings.append(f"{k}: dropped {int(dup.sum()):,} duplicate rows "
                            f"(same school in more than one file)")
            df = df[~dup].reset_index(drop=True)
        tables[k] = df
    return tables, warnings
