"""Check the DAX measures of the open Power BI model against the SQL re-implementation.

    python scripts/verify_powerbi_model.py              # model already loaded in Desktop
    python scripts/verify_powerbi_model.py --refresh    # load the CSVs first (TMSL refresh)

Needs Power BI Desktop with powerbi/RajasthanUDISE.pbip open and refreshed (local only: the
model holds school-level data). Desktop runs a local Analysis Services engine; its port is in
msmdsrv.port.txt in Desktop's workspace folder. This script sends DAX queries to it through
ADOMD.NET, Microsoft's client library, from PowerShell: by default the copy that ships with
Power BI Desktop (Microsoft.PowerBI.AdomdClient.dll), or the DLL given with --adomd. Every value
is compared with sql/powerbi/check_*.sql on the exported CSVs (the same numbers as
powerbi/reconciliation.xlsx). Writes powerbi/model_check.md.
"""
import argparse
import json
import math
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pipeline import config as C  # noqa: E402
from scripts import make_reconciliation_workbook as R  # noqa: E402

OUT = C.ROOT / "powerbi" / "model_check.md"
RUN_DAX = r"""
param($Dll, $Port, $QueryFile, $OutFile)
Add-Type -Path $Dll
$con = New-Object Microsoft.AnalysisServices.AdomdClient.AdomdConnection "Data Source=localhost:$Port"
$con.Open()
$cmd = $con.CreateCommand()
$cmd.CommandText = [IO.File]::ReadAllText($QueryFile)
$cmd.CommandTimeout = 600
$r = $cmd.ExecuteReader()
$rows = New-Object System.Collections.ArrayList
while ($r.Read()) {
    $o = [ordered]@{}
    for ($i = 0; $i -lt $r.FieldCount; $i++) {
        $v = $r.GetValue($i); if ($v -is [DBNull]) { $v = $null }
        $o[$r.GetName($i)] = $v
    }
    [void]$rows.Add($o)
}
$con.Close()
ConvertTo-Json -InputObject @($rows) -Depth 3 | Set-Content -Encoding utf8 $OutFile
"""
REFRESH = r"""
param($Dll, $Port)
Add-Type -Path $Dll
$con = New-Object Microsoft.AnalysisServices.AdomdClient.AdomdConnection "Data Source=localhost:$Port"
$con.Open()
$cmd = $con.CreateCommand()
$cmd.CommandText = 'SELECT [CATALOG_NAME] FROM $SYSTEM.DBSCHEMA_CATALOGS'
$r = $cmd.ExecuteReader(); [void]$r.Read(); $db = $r.GetValue(0); $r.Close()
$cmd.CommandText = '{ "refresh": { "type": "full", "objects": [ { "database": "' + $db + '" } ] } }'
$cmd.CommandTimeout = 1800
[void]$cmd.ExecuteNonQuery()
$con.Close()
"""
YOY = {  # DAX measure -> (SQL column, "pct" = relative change, "diff" = difference)
    "Enrolment 1-12 YoY %": ("enrolment_1_12", "pct"), "Teachers YoY %": ("teachers", "pct"),
    "PTR YoY Change": ("ptr", "diff"), "Single-Teacher Schools % YoY Change": ("pct_single_teacher", "diff"),
    "Teacher Shortfall YoY Change": ("teacher_shortfall", "diff"),
}
VS_STATE = {"PTR vs State": "ptr", "Single-Teacher Schools % vs State": "pct_single_teacher",
            "Infrastructure Index vs State": "avg_infra_index", "Girls' Share 1-12 vs State": "girls_share",
            "Teacher Shortfall % vs State": "teacher_shortfall_pct"}


def find_adomd() -> Path:
    """ADOMD.NET client shipped with Power BI Desktop (Microsoft Store or installer version)."""
    r = subprocess.run(["powershell", "-NoProfile", "-Command",
                        "(Get-AppxPackage -Name Microsoft.MicrosoftPowerBIDesktop).InstallLocation"],
                       capture_output=True, text=True)
    folders = [Path(r.stdout.strip()) / "bin"] if r.stdout.strip() else []
    folders.append(Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Microsoft Power BI Desktop" / "bin")
    for f in folders:
        if (f / "Microsoft.PowerBI.AdomdClient.dll").exists():
            return f / "Microsoft.PowerBI.AdomdClient.dll"
    raise SystemExit("Power BI Desktop's ADOMD.NET client not found: pass --adomd <path to the DLL>")


def desktop_port() -> int:
    roots = [Path(os.environ["LOCALAPPDATA"]) / "Microsoft" / "Power BI Desktop" / "AnalysisServicesWorkspaces",
             Path(os.environ["USERPROFILE"]) / "Microsoft" / "Power BI Desktop Store App" / "AnalysisServicesWorkspaces"]
    files = [p for r in roots if r.exists() for p in r.glob("*/Data/msmdsrv.port.txt")]
    if not files:
        raise SystemExit("No running Power BI Desktop model found: open powerbi/RajasthanUDISE.pbip first")
    raw = max(files, key=lambda p: p.stat().st_mtime).read_bytes()
    return int((raw.decode("utf-16") if b"\x00" in raw else raw.decode()).strip())


def run_dax(query: str, dll: Path, port: int) -> pd.DataFrame:
    with tempfile.TemporaryDirectory() as d:
        ps, qf, out = Path(d) / "q.ps1", Path(d) / "q.dax", Path(d) / "out.json"
        ps.write_text(RUN_DAX, encoding="utf-8")
        qf.write_text(query, encoding="utf-8")
        r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ps),
                            "-Dll", str(dll), "-Port", str(port), "-QueryFile", str(qf), "-OutFile", str(out)],
                           capture_output=True, text=True)
        if r.returncode or not out.exists():
            raise RuntimeError(r.stdout + r.stderr)
        rows = json.loads(out.read_text(encoding="utf-8-sig"))
    df = pd.DataFrame(rows)
    df.columns = [c.split("[")[-1].rstrip("]") for c in df.columns]  # 'dim_year[academic_year]' -> 'academic_year'
    return df


def refresh(dll: Path, port: int) -> float:
    """Full refresh of the open model (what Home > Refresh does): Desktop does not always load the
    data when it opens a project. Returns the seconds it took."""
    t0 = time.perf_counter()
    with tempfile.TemporaryDirectory() as d:
        ps = Path(d) / "refresh.ps1"
        ps.write_text(REFRESH, encoding="utf-8")
        r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ps),
                            "-Dll", str(dll), "-Port", str(port)], capture_output=True, text=True)
        if r.returncode:
            raise RuntimeError(r.stdout + r.stderr)
    return round(time.perf_counter() - t0, 1)


def dax_name(name: str) -> str:
    return "[" + name.replace("]", "]]") + "]"


def same(a, b) -> bool:
    if a is None or b is None or (isinstance(a, float) and math.isnan(a)) or (isinstance(b, float) and math.isnan(b)):
        return (a is None or (isinstance(a, float) and math.isnan(a))) and \
               (b is None or (isinstance(b, float) and math.isnan(b)))
    return math.isclose(float(a), float(b), rel_tol=1e-9, abs_tol=1e-9)


def verify(dll: Path, gold_db: Path = C.GOLD_DB, csv_dir: Path = R.CSV_DIR) -> list[tuple[str, int, int, str]]:
    port = desktop_port()
    ref = R.compute(gold_db, csv_dir)
    sql = ref["measures"]
    state = sql[sql["district"].isna()].set_index("year")
    results = []

    measures = ", ".join(f'"{col}", {dax_name(name)}' for col, name, _ in R.MEASURES)
    dax_state = run_dax(f"EVALUATE SUMMARIZECOLUMNS ( dim_year[academic_year], {measures} )", dll, port)
    bad = [(y, c) for _, row in dax_state.iterrows() for c, _, _ in R.MEASURES
           if not same(row[c], state.loc[row["academic_year"], c]) for y in [row["academic_year"]]]
    results.append(("State value of every district and resource-gap measure", len(dax_state) * len(R.MEASURES),
                    len(bad), ", ".join(f"{c} {y}" for y, c in bad[:5])))

    vs = ", ".join(f'"{k}", {dax_name(k)}' for k in VS_STATE)
    dax_dist = run_dax(f"EVALUATE SUMMARIZECOLUMNS ( dim_year[academic_year], dim_district[district_name], "
                       f"{measures}, {vs} )", dll, port)
    dist = sql.dropna(subset=["district"]).set_index(["district", "year"])
    bad, n = [], 0
    for _, row in dax_dist.iterrows():
        key = (row["district_name"], row["academic_year"])
        for c, _, _ in R.MEASURES:
            n += 1
            if not same(row[c], dist.loc[key, c]):
                bad.append(f"{c} {key}")
        for k, c in VS_STATE.items():
            n += 1
            if not same(row[k], dist.loc[key, c] - state.loc[key[1], c]):
                bad.append(f"{k} {key}")
    results.append((f"District values ({len(dax_dist)} district-years), incl. the 'vs State' measures", n, len(bad),
                    ", ".join(bad[:5])))

    years = list(state.index)
    yoy = ", ".join(f'"{k}", {dax_name(k)}' for k in YOY)
    dax_yoy = run_dax(f"EVALUATE SUMMARIZECOLUMNS ( dim_year[academic_year], {yoy} )", dll, port).set_index("academic_year")
    bad = []
    for k, (c, kind) in YOY.items():
        cur, prev = state.loc[years[1], c], state.loc[years[0], c]
        expected = (cur - prev) / prev if kind == "pct" else cur - prev
        if not same(dax_yoy.loc[years[1], k], expected):
            bad.append(k)
        if years[0] in dax_yoy.index and not same(dax_yoy.loc[years[0], k], None):
            bad.append(f"{k} {years[0]} should be blank")
    results.append((f"Year-over-year measures ({years[1]} vs {years[0]}; blank for {years[0]})", 2 * len(YOY),
                    len(bad), ", ".join(bad)))

    dax_val = run_dax('EVALUATE SUMMARIZECOLUMNS ( official_totals[year], official_totals[indicator], '
                      '"pipeline", [Pipeline Value], "official", [Official Value], "status", [Match Status] )',
                      dll, port)
    expected = ref["pipeline"].set_index(["year", "indicator"])["value"]
    bad = [f"{r.indicator} {r.year}" for r in dax_val.itertuples() if not same(r.pipeline, expected.loc[(r.year, r.indicator)])]
    results.append(("Pipeline Value = pipeline/validation.py on gold, every year x indicator", len(dax_val), len(bad),
                    ", ".join(bad)))
    published = dax_val.dropna(subset=["official"])
    not_exact = published[published["status"] != "exact match"]
    results.append(("Published figures with Match Status 'exact match'", len(published), len(not_exact),
                    ", ".join(not_exact["indicator"])))
    summary = run_dax('EVALUATE ROW ( "summary", [Validation Summary] )', dll, port).iloc[0]["summary"]
    results.append((f"Validation Summary reads: {summary}", 1, 0 if summary.startswith(f"{len(published)} of ") else 1, ""))
    return results


def to_markdown(results) -> str:
    total, failed = sum(r[1] for r in results), sum(r[2] for r in results)
    lines = ["# Power BI model check", "",
             "Generated by `scripts/verify_powerbi_model.py`: DAX queries against the model open in Power BI "
             "Desktop (`powerbi/RajasthanUDISE.pbip`), compared with the SQL re-implementation of the measures "
             "on the exported CSVs (`sql/powerbi/`, the values in `reconciliation.xlsx`).", "",
             f"**{total - failed:,} of {total:,} values agree** (tolerance 1e-9).", "",
             "| Check | Values | Differences | Detail |", "|---|---:|---:|---|"]
    lines += [f"| {name} | {n:,} | {bad} | {detail} |" for name, n, bad, detail in results]
    return "\n".join(lines + ["", f"{C.SOURCE_NOTE}.", ""])


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--adomd", type=Path, default=None,
                   help="ADOMD.NET client DLL (default: the one that ships with Power BI Desktop)")
    p.add_argument("--gold-db", type=Path, default=C.GOLD_DB)
    p.add_argument("--refresh", action="store_true", help="load the CSVs into the open model first")
    a = p.parse_args()
    dll = a.adomd or find_adomd()
    if a.refresh:
        print(f"  refreshed the model in {refresh(dll, desktop_port())} s")
    results = verify(dll, a.gold_db)
    OUT.write_text(to_markdown(results), encoding="utf-8")
    for name, n, bad, detail in results:
        print(f"  {'PASS' if not bad else 'FAIL'}  {name}: {n} values, {bad} differ {detail}")
    sys.exit(1 if any(r[2] for r in results) else 0)


if __name__ == "__main__":
    main()
