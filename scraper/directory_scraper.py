"""
Phase 1 — Segal Funder Directory Scraper
=========================================
Scrapes all funders from the Segal Family Foundation directory, filters to
those tagged "Quality Education" AND working in Africa, then writes them to
the `funders` table.

Run once (or manually when you want to refresh the list):
    python -m scraper.directory_scraper

Optional flags:
    --dry-run    Print results without writing to the database
    --debug      Show browser window (non-headless) for inspection
"""

import argparse
import csv
import sys
import time
from dataclasses import dataclass, field

from playwright.sync_api import Page, sync_playwright
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

import config
from db.models import Base, Funder

DIRECTORY_URL = "https://www.segalfamilyfoundation.org/tools/funder-directory/"
TABLE_SELECTOR = "#footable_31703"

# How long to wait (ms) for the table to appear after page load
TABLE_TIMEOUT_MS = 20_000

# Column name fragments to search for in table headers (case-insensitive).
# We detect columns dynamically because we can't inspect the live page ahead of time.
HEADER_ALIASES = {
    "name":        ["funder", "name", "organization"],
    "website_url": ["url", "website", "link", "site"],
    "focus_areas": ["sector", "sdg", "thematic"],   # "focus" removed — too generic, matches "Geographic Focus"
    "geography":   ["geographic", "geography", "region", "location", "country"],  # "geographic" first so it wins over "sector"
}


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class RawFunder:
    name: str
    website_url: str
    focus_areas: list[str] = field(default_factory=list)
    geography: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Filter logic (driven by config.py)
# ---------------------------------------------------------------------------

def _matches_education(focus_areas: list[str]) -> bool:
    lowered = [f.lower().strip() for f in focus_areas]
    return any(
        sector in text
        for sector in config.EDUCATION_SECTORS
        for text in lowered
    )


def _matches_africa(geography: list[str]) -> bool:
    lowered = [g.lower().strip() for g in geography]
    return any(
        keyword in text
        for keyword in config.AFRICA_KEYWORDS
        for text in lowered
    )


def classify(funder: RawFunder) -> str:
    """Return 'both', 'education_only', 'africa_only', or 'none'."""
    edu = _matches_education(funder.focus_areas)
    africa = _matches_africa(funder.geography)
    if edu and africa:
        return "both"
    if edu:
        return "education_only"
    if africa:
        return "africa_only"
    return "none"


# ---------------------------------------------------------------------------
# Column detection
# ---------------------------------------------------------------------------

def detect_columns(headers: list[str]) -> dict[str, int]:
    """
    Map logical field names → column index by fuzzy-matching header text
    against HEADER_ALIASES.
    Returns a dict like {"name": 0, "website_url": 2, ...}.
    Missing columns are excluded (caller should warn).
    """
    mapping: dict[str, int] = {}
    for idx, header in enumerate(headers):
        h = header.lower().strip()
        for field_name, aliases in HEADER_ALIASES.items():
            if field_name not in mapping and any(alias in h for alias in aliases):
                mapping[field_name] = idx
    return mapping


# ---------------------------------------------------------------------------
# Playwright scraping helpers
# ---------------------------------------------------------------------------

def _wait_for_table(page: Page) -> None:
    """Wait for the FooTable to render at least one data row."""
    page.wait_for_selector(f"{TABLE_SELECTOR} tbody tr", timeout=TABLE_TIMEOUT_MS)


def _set_large_page_size(page: Page) -> None:
    """
    Try to set the table's per-page size to the largest finite value available
    (e.g. 100). We deliberately avoid "-1" (show all) because the table uses
    server-side pagination — selecting "all" only surfaces the rows already
    loaded in the browser for the current page, not the full dataset.
    We always paginate regardless, but fewer pages means fewer clicks.
    """
    selectors = [
        f"{TABLE_SELECTOR} select.footable-page-size",
        "select[name='footable_31703_length']",
        ".footable-filtering select",
        "select.ninja-table-page-size",
    ]
    for sel in selectors:
        try:
            dropdown = page.locator(sel).first
            if dropdown.count() == 0:
                continue
            options = dropdown.locator("option").all()
            values = [o.get_attribute("value") or "" for o in options]
            # Pick the largest finite (positive) value
            numeric = sorted(
                [int(v) for v in values if v.lstrip("-").isdigit() and int(v) > 0],
                reverse=True,
            )
            if numeric:
                dropdown.select_option(str(numeric[0]))
                page.wait_for_load_state("networkidle", timeout=10_000)
                _wait_for_table(page)
                print(f"  → Set page size to {numeric[0]} rows.")
            return
        except Exception:
            continue


def _get_headers(page: Page) -> list[str]:
    """
    Extract real column header text from the table thead.
    Skips filter-row cells — FooTable renders a search/filter <tr> above the
    real header row whose single <th> spans all columns and contains the full
    list of filter options as multiline text. We drop any cell whose text
    contains a newline or is longer than 80 chars.
    """
    headers = page.locator(f"{TABLE_SELECTOR} thead th").all()
    result = []
    for h in headers:
        text = h.inner_text().strip()
        if "\n" not in text and len(text) <= 80:
            result.append(text)
    return result


def _scrape_current_page(page: Page, col_map: dict[str, int]) -> list[RawFunder]:
    """Extract all funder rows visible on the current page."""
    funders: list[RawFunder] = []
    rows = page.locator(f"{TABLE_SELECTOR} tbody tr").all()

    for row in rows:
        cells = row.locator("td").all()
        if not cells:
            continue

        def cell_text(field: str) -> str:
            idx = col_map.get(field)
            if idx is None or idx >= len(cells):
                return ""
            return cells[idx].inner_text().strip()

        def cell_link(field: str) -> str:
            idx = col_map.get(field)
            if idx is None or idx >= len(cells):
                return ""
            anchor = cells[idx].locator("a").first
            if anchor.count() > 0:
                return anchor.get_attribute("href") or cell_text(field)
            return cell_text(field)

        name = cell_text("name")
        if not name:
            continue  # skip empty/spacer rows

        url = cell_link("website_url") or cell_text("website_url")

        # Focus areas and geography may be comma- or semicolon-separated
        raw_focus = cell_text("focus_areas")
        raw_geo = cell_text("geography")

        focus_areas = [f.strip() for f in raw_focus.replace(";", ",").split(",") if f.strip()]
        geography = [g.strip() for g in raw_geo.replace(";", ",").split(",") if g.strip()]

        funders.append(RawFunder(
            name=name,
            website_url=url,
            focus_areas=focus_areas,
            geography=geography,
        ))

    return funders


def _has_next_page(page: Page) -> bool:
    """Return True if a 'next page' control exists and is not disabled."""
    # FooTable uses <li class="footable-page-nav"> with data-page="next"
    next_btn = page.locator(
        f"{TABLE_SELECTOR} tfoot [data-page='next']:not(.disabled)"
    ).first
    if next_btn.count() > 0:
        return True
    # Fallback: look for a generic "next" link
    next_link = page.locator(
        f"{TABLE_SELECTOR} tfoot a:has-text('next'):not(.disabled)"
    ).first
    return next_link.count() > 0


def _click_next_page(page: Page) -> None:
    """Click the 'next page' button and wait for the table to re-render."""
    clicked = False
    for selector in [
        f"{TABLE_SELECTOR} tfoot [data-page='next']",
        f"{TABLE_SELECTOR} tfoot a:has-text('next')",
        f"{TABLE_SELECTOR} tfoot a:has-text('›')",
        f"{TABLE_SELECTOR} tfoot a:has-text('»')",
    ]:
        btn = page.locator(selector).first
        if btn.count() > 0:
            btn.click()
            clicked = True
            break

    if not clicked:
        raise RuntimeError("Could not find or click the next-page button.")

    # Brief pause then wait for table to re-render
    time.sleep(0.8)
    _wait_for_table(page)


# ---------------------------------------------------------------------------
# Main scrape loop
# ---------------------------------------------------------------------------

def scrape_all(page: Page) -> list[RawFunder]:
    """Navigate the full directory and return every funder entry found."""
    print(f"Loading {DIRECTORY_URL} …")
    page.goto(DIRECTORY_URL, wait_until="networkidle", timeout=60_000)
    _wait_for_table(page)
    print("Table rendered.")

    # Read column headers once
    headers = _get_headers(page)
    print(f"Detected columns: {headers}")
    col_map = detect_columns(headers)
    print(f"Column mapping:   {col_map}")

    # Warn if any expected column is missing
    for field_name in HEADER_ALIASES:
        if field_name not in col_map:
            print(
                f"  WARNING: could not detect column '{field_name}'. "
                "Check HEADER_ALIASES in the scraper if data is missing."
            )

    all_funders: list[RawFunder] = []
    page_num = 1

    while True:
        print(f"  Scraping page {page_num} …", end=" ", flush=True)
        batch = _scrape_current_page(page, col_map)
        print(f"{len(batch)} rows")
        all_funders.extend(batch)

        if not _has_next_page(page):
            break

        _click_next_page(page)
        page_num += 1

    return all_funders


# ---------------------------------------------------------------------------
# Database persistence
# ---------------------------------------------------------------------------

def save_to_db(funders: list[RawFunder], session: Session) -> dict[str, int]:
    """
    Upsert funders into the database.
    - Both-filter match → is_active=True
    - Single-filter match → is_active=False (preserved, not crawled)
    - No match → skipped entirely

    Returns a counts dict for reporting.
    """
    counts = {"active": 0, "inactive": 0, "skipped": 0, "updated": 0}

    for raw in funders:
        classification = classify(raw)

        if classification == "none":
            counts["skipped"] += 1
            continue

        is_active = classification == "both"

        # Check if already in DB (by name, tolerant of URL changes)
        existing = session.scalar(
            select(Funder).where(Funder.name == raw.name)
        )

        if existing:
            existing.website_url = raw.website_url
            existing.focus_areas = raw.focus_areas
            existing.geography = raw.geography
            existing.is_active = is_active
            counts["updated"] += 1
        else:
            session.add(Funder(
                name=raw.name,
                website_url=raw.website_url,
                focus_areas=raw.focus_areas,
                geography=raw.geography,
                is_active=is_active,
            ))
            if is_active:
                counts["active"] += 1
            else:
                counts["inactive"] += 1

    session.commit()
    return counts


# ---------------------------------------------------------------------------
# CSV export
# ---------------------------------------------------------------------------

def export_education_funders(output_path: str = "education_funders.csv") -> None:
    """
    Query the DB for all funders with an education focus and write them to CSV.
    Includes both active funders (education + Africa) and education-only funders.
    """
    engine = create_engine(config.DATABASE_URL)
    with Session(engine) as session:
        funders = session.execute(select(Funder)).scalars().all()

    education_funders = [
        f for f in funders
        if _matches_education(f.focus_areas or [])
    ]
    education_funders.sort(key=lambda f: f.name.lower())

    with open(output_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["Name", "Website", "Also Covers Africa"])
        for f in education_funders:
            covers_africa = "Yes" if _matches_africa(f.geography or []) else "No"
            writer.writerow([f.name, f.website_url or "", covers_africa])

    print(f"Exported {len(education_funders)} education funders → {output_path}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(dry_run: bool = False, debug: bool = False) -> None:
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not debug)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
        )
        page = context.new_page()

        try:
            all_funders = scrape_all(page)
        finally:
            browser.close()

    print(f"\nTotal scraped: {len(all_funders)}")

    # Classify
    both, edu_only, africa_only, none_ = [], [], [], []
    for f in all_funders:
        c = classify(f)
        if c == "both":
            both.append(f)
        elif c == "education_only":
            edu_only.append(f)
        elif c == "africa_only":
            africa_only.append(f)
        else:
            none_.append(f)

    print(f"  ✓ Both filters (will be crawled):   {len(both)}")
    print(f"  ~ Education only (stored inactive): {len(edu_only)}")
    print(f"  ~ Africa only (stored inactive):    {len(africa_only)}")
    print(f"  ✗ No match (skipped):               {len(none_)}")

    if dry_run:
        print("\n[dry-run] Sample of active funders:")
        for f in both[:10]:
            print(f"  {f.name} | {f.website_url}")
            print(f"    focus: {f.focus_areas}")
            print(f"    geo:   {f.geography}")
        print("\n[dry-run] Database not modified.")
        return

    engine = create_engine(config.DATABASE_URL)
    Base.metadata.create_all(engine)  # no-op if tables exist

    with Session(engine) as session:
        counts = save_to_db(all_funders, session)

    print(f"\nDatabase updated:")
    print(f"  New active funders:   {counts['active']}")
    print(f"  New inactive funders: {counts['inactive']}")
    print(f"  Updated existing:     {counts['updated']}")
    print(f"  Skipped (no match):   {counts['skipped']}")
    print("\nDone.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Scrape the Segal funder directory.")
    parser.add_argument("--dry-run", action="store_true", help="Print results, skip DB write")
    parser.add_argument("--debug", action="store_true", help="Show browser window")
    parser.add_argument("--export", metavar="FILE", nargs="?", const="education_funders.csv",
                        help="Export education funders from DB to CSV (no scraping). "
                             "Default filename: education_funders.csv")
    args = parser.parse_args()

    try:
        if args.export:
            export_education_funders(args.export)
        else:
            main(dry_run=args.dry_run, debug=args.debug)
    except Exception as e:
        print(f"\nError: {e}", file=sys.stderr)
        sys.exit(1)
