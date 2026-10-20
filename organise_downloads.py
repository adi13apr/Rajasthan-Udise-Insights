"""Move UDISE+ zips from your Downloads folder into data/raw/<year>/.

Usage:  python organise_downloads.py "C:\\Users\\<you>\\Downloads"
Only files whose names match the portal's pattern (e.g. facility_data_KOTA_2024-25.zip)
are moved. Files already in place are skipped, so it is safe to run repeatedly.
"""
import re
import shutil
import sys
from pathlib import Path

RAW = Path(__file__).resolve().parent / "data" / "raw"
PATTERN = re.compile(r"^(profile_data_[12]|facility_data|teacher_data|enrolment_data_[12]|safety)_(.+)_(\d{4}-\d{2})\.zip$")

src = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.home() / "Downloads"
moved = skipped = 0
for f in sorted(src.glob("*.zip")):
    m = PATTERN.match(f.name)
    if not m:
        continue
    dest = RAW / m.group(3) / f.name
    if dest.exists():
        skipped += 1
        continue
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(f), dest)
    moved += 1

print(f"Moved {moved} files, skipped {skipped} already in place.")
for year_dir in sorted(p for p in RAW.iterdir() if p.is_dir()):
    districts = {PATTERN.match(z.name).group(2) for z in year_dir.glob("*.zip") if PATTERN.match(z.name)}
    print(f"{year_dir.name}: {len(districts)} districts -> {', '.join(sorted(districts))}")
