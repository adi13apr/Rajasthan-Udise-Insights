"""Download UDISE+ district files for Rajasthan from the Data Sharing Portal.

Polite by design: you log in yourself, it pauses between requests, it only
fetches the 4 files the analysis needs, and it skips anything already downloaded.

Setup (once):
    pip install playwright
    python -m playwright install chromium

Whole state, both years (uses the portal's "All" district option):
    python download_udise.py --years 2024-25 2025-26
One kind of file, district by district (e.g. when the state-wide file is not available):
    python download_udise.py --years 2024-25 2025-26 --by-district --files Enrollment-1
One district only:
    python download_udise.py --years 2024-25 --district KOTA
"""
import argparse
import random
import time
from pathlib import Path

from playwright.sync_api import sync_playwright, Error as PWError, TimeoutError as PWTimeout

PORTAL = "https://microdata.udiseplus.gov.in/"
STATE = "RAJASTHAN"
REMARK = ("Student research project analysing district-level school infrastructure, "
          "teacher and enrolment indicators in Rajasthan.")
# Card title on the portal -> start of the downloaded file name
FILES = {"Profile-1": "profile_data_1", "Enrollment-1": "enrolment_data_1",
         "Facility": "facility_data", "Teacher": "teacher_data"}
RAW = Path(__file__).resolve().parent / "data" / "raw"
LOG = Path(__file__).resolve().parent / "download_log.txt"


def log(msg):
    print(msg)
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(msg + "\n")


def find_form_page(context):
    """Use whichever tab has the download form open (login may open a new tab)."""
    for pg in reversed(context.pages):
        if "csvdata" in pg.url.lower():
            return pg
    pg = context.pages[-1]
    pg.goto(PORTAL + "#/CSVdata")  # logged-in session should open the form directly
    return pg


def wait_for_form(page):
    try:
        page.locator("select").first.wait_for(state="attached", timeout=20_000)
    except PWTimeout:
        debug = Path(__file__).resolve().parent / "debug"
        debug.mkdir(exist_ok=True)
        page.screenshot(path=str(debug / "page.png"), full_page=True)
        (debug / "page.html").write_text(page.content(), encoding="utf-8")
        frames = [f.url for f in page.frames]
        raise SystemExit(
            f"\nCould not find the form's dropdowns.\n  Tab URL: {page.url}\n"
            f"  Open tabs: {[p.url for p in page.context.pages]}\n  Frames: {frames}\n"
            f"Saved debug/page.png and debug/page.html - upload them so the selectors can be fixed.")


def handle_alert(page, context):
    """Deal with SweetAlert pop-ups (session expiry, server messages). Returns the page to use."""
    popup = page.locator(".swal2-popup")
    if not popup.count() or not popup.first.is_visible():
        return page
    text = popup.first.inner_text().strip().replace("\n", " ")
    log(f"  Portal pop-up: {text}")
    confirm = page.locator(".swal2-confirm")
    if confirm.count() and confirm.first.is_visible():
        confirm.first.click()
    if any(w in text.lower() for w in ("session", "login", "log in", "expired", "unauthor")):
        input("  Looks like the session ended. Log in again in the browser window, open "
              "'Download Education Data (CSV)', then press Enter here...")
        page = find_form_page(context)
        wait_for_form(page)
        dismiss_policy_popup(page)
    return page


def dismiss_policy_popup(page):
    btn = page.get_by_role("button", name="I Understand")
    if btn.is_visible():
        btn.click()


def district_options(page):
    sel = page.locator("select").nth(2)
    labels, stable = [], 0
    for _ in range(40):  # wait until the list stops growing (up to ~20 s)
        now = [o.strip() for o in sel.locator("option").all_inner_texts()]
        now = [l for l in now if l and not l.lower().startswith("select")]
        stable = stable + 1 if (now and now == labels) else 0
        labels = now
        if stable >= 4:
            return labels
        time.sleep(0.5)
    if labels:
        return labels
    raise RuntimeError("District list did not load")


def file_token(district):
    """'All' downloads the whole state, and the portal names those files after the state."""
    return STATE if district.lower() == "all" else district.replace(" ", "").upper()


def already_have(year, district, titles):
    folder = RAW / year
    existing = {f.name.replace(" ", "").upper() for f in folder.glob("*.zip")}
    return all(f"{FILES[t]}_{file_token(district)}_{year}.zip".upper() in existing for t in titles)


def request_files(page, year, district):
    selects = page.locator("select")
    selects.nth(0).select_option(label=year)
    selects.nth(1).select_option(label=STATE)
    district_options(page)
    selects.nth(2).select_option(label=district)
    remark = page.locator("textarea[name=purpose]")
    if remark.input_value() != REMARK:
        remark.fill(REMARK)
    box = page.locator("input[type=checkbox]")
    if not box.is_checked():
        box.check()
        dismiss_policy_popup(page)
    page.get_by_role("button", name="Get Files").click()
    page.get_by_text("Files Ready For Download").wait_for(timeout=60_000)
    time.sleep(2)  # let the cards refresh for the newly selected district


def download_card(page, title, year, district):
    xpath = (f"//*[normalize-space(.)='{title}']"
             "/ancestor::*[.//button[contains(., 'Download CSV File')]][1]"
             "//button[contains(., 'Download CSV File')]")
    with page.expect_download(timeout=120_000) as dl:
        page.locator(f"xpath={xpath}").first.click()
    d = dl.value
    name = d.suggested_filename
    if file_token(district) not in name.replace(" ", "").upper() or year not in name:
        raise RuntimeError(f"Got '{name}' while expecting {district} {year} (page not refreshed?)")
    dest = RAW / year / name
    dest.parent.mkdir(parents=True, exist_ok=True)
    d.save_as(dest)
    return name


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", nargs="+", default=["2024-25", "2025-26"])
    ap.add_argument("--limit", type=int, default=0, help="only do the first N districts (for testing)")
    ap.add_argument("--district", help="only this district, e.g. KOTA")
    ap.add_argument("--files", nargs="+", choices=list(FILES), default=list(FILES),
                    help="which cards to download, e.g. --files Enrollment-1")
    ap.add_argument("--by-district", action="store_true",
                    help="download each district separately instead of the whole state at once")
    args = ap.parse_args()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(accept_downloads=True)
        page = context.new_page()
        page.goto(PORTAL)
        input("\nLog in in the browser window, open 'Download Education Data (CSV)', "
              "then press Enter here...")
        page = find_form_page(context)
        print(f"Using tab: {page.url}")
        wait_for_form(page)
        dismiss_policy_popup(page)

        for year in args.years:
            page.locator("select").nth(0).select_option(label=year)
            page.locator("select").nth(1).select_option(label=STATE)
            districts = district_options(page)
            log(f"\n{year}: {len(districts)} districts listed -> {', '.join(districts)}")
            if "All" in districts and not (args.by_district or args.district):
                districts = ["All"]  # one request gives the whole state
            else:
                districts = [d for d in districts if d.lower() != "all"]
            if args.district:
                districts = [d for d in districts if d.upper() == args.district.upper()]
            if args.limit:
                districts = districts[:args.limit]

            for district in districts:
                if already_have(year, district, args.files):
                    log(f"  skip {district} (already downloaded)")
                    continue
                for attempt in (1, 2, 3):
                    try:
                        page = handle_alert(page, context)
                        request_files(page, year, district)
                        for title in args.files:
                            log(f"  {district}: saved {download_card(page, title, year, district)}")
                            time.sleep(random.uniform(1.5, 3))
                        break
                    except (PWError, RuntimeError) as e:
                        log(f"  {district}: attempt {attempt} failed -> {str(e).splitlines()[0]}")
                        time.sleep(10)
                        page = handle_alert(page, context)
                else:
                    log(f"  FAILED {district} {year}: download it manually")
                time.sleep(random.uniform(4, 8))  # be gentle with the server

        browser.close()
    log("Done. Run `python run_pipeline.py` and check reports/qa_report.md for missing files.")


if __name__ == "__main__":
    main()