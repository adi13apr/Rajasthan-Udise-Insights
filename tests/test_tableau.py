"""The generated Tableau workbook is consistent (no Tableau needed to run these tests)."""
import xml.etree.ElementTree as ET
import zipfile

import pytest

pytest.importorskip("tableauhyperapi")
from tableauhyperapi import Connection, HyperProcess, Telemetry  # noqa: E402

import scripts.make_tableau_workbook as mk  # noqa: E402


@pytest.fixture(scope="module")
def twb():
    return ET.fromstring(mk.build_twb())


def test_sheets_dashboards_and_parameters(twb):
    sheets = {w.get("name") for w in twb.iter("worksheet")}
    dashboards = {d.get("name") for d in twb.iter("dashboard")}
    assert len(sheets) == 11 and dashboards == {"Overview", "Girls in secondary", "Teachers"}
    params = {c.get("caption") for ds in twb.iter("datasource") if ds.get("name") == "Parameters" for c in ds.iter("column")}
    assert params == {"Year", "Indicator"}


def test_every_dashboard_zone_and_window_names_a_real_sheet(twb):
    sheets = {w.get("name") for w in twb.iter("worksheet")}
    for d in twb.iter("dashboard"):
        for z in d.iter("zone"):
            if z.get("name"):
                assert z.get("name") in sheets, z.get("name")
    for w in twb.iter("window"):
        assert w.get("name") in sheets | {"Overview", "Girls in secondary", "Teachers"}


def test_every_field_on_a_shelf_is_declared(twb):
    for ws in twb.iter("worksheet"):
        declared = {ci.get("name") for ci in ws.iter("column-instance")}
        for shelf in ("rows", "cols"):
            text = ws.find(f".//{shelf}").text or ""
            for ref in [part.split("].")[-1] for part in text.split(" / ") if part]:
                assert ref in declared, (ws.get("name"), ref)


def test_extracts_hold_every_row(tmp_path):
    files = mk.write_extracts(tmp_path)
    with HyperProcess(Telemetry.DO_NOT_SEND_USAGE_DATA_TO_TABLEAU, parameters={"log_dir": str(tmp_path)}) as hp:
        for key, path in files.items():
            with Connection(hp.endpoint, str(path)) as con:
                n = con.execute_scalar_query('SELECT COUNT(*) FROM "Extract"."Extract"')
            assert n == len(mk.DFS[key]), key


def test_packaged_workbook_contents():
    if not mk.OUT.exists():
        pytest.skip("run scripts/make_tableau_workbook.py first")
    names = set(zipfile.ZipFile(mk.OUT).namelist())
    assert mk.TWB_NAME in names and {f"{mk.EXTRACT_DIR}/{k}.hyper" for k in mk.FILES} <= names
