"""
Phase 3 — Weekly Site Crawler
==============================
Visits every active funder's website, discovers grant/news pages via:
  1. Fixed path probing  (/grants, /funding, /news, /blog, etc.)
  2. Homepage link discovery (catches /news&insights, /news-and-updates, etc.)

Extracts page text, computes a SHA-256 hash, compares against the previous
snapshot, and saves every visit to `page_snapshots`. Changed pages are
picked up by Phase 4 (the Claude AI filter).

Run as part of the weekly pipeline:
    python -m scraper.site_crawler

Or standalone for testing:
    python -m scraper.site_crawler --limit 5    # only crawl 5 funders
    python -m scraper.site_crawler --funder-id 42
"""

import argparse
import asyncio
import hashlib
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from playwright.async_api import Browser, BrowserContext, async_playwright
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

import config
from db.models import Base, Funder, PageSnapshot

# Keywords used to identify grant/news-related links during homepage discovery.
# Checked against both the href URL and the link's visible text.
LINK_KEYWORDS = [
    "grant", "fund", "opportunit", "apply", "call", "rfp",
    "news", "blog", "announcement", "update", "insight", "award",
    "programme", "program", "initiative", "open",
]

# Max pages to crawl per funder (homepage + discovered + probed).
# Prevents runaway crawling on sites with many matching links.
MAX_PAGES_PER_FUNDER = 5


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class CrawlResult:
    funder_id: int
    url: str
    content_text: str
    content_hash: str
    status: str                    # ok | error | blocked
    error_message: str | None = None


@dataclass
class CrawlSummary:
    total_funders: int = 0
    ok: int = 0
    errors: int = 0
    blocked: int = 0
    changed: int = 0               # pages whose hash differs from last snapshot
    unchanged: int = 0
    new_pages: int = 0             # pages with no previous snapshot (first run)
    results: list[CrawlResult] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Text extraction
# ---------------------------------------------------------------------------

def _extract_text(html: str) -> str:
    """
    Parse page HTML with BeautifulSoup, remove boilerplate (nav, footer,
    cookie banners, scripts), and return clean visible text.
    """
    soup = BeautifulSoup(html, "html.parser")

    # Remove noise tags entirely
    for tag in soup(["nav", "footer", "header", "script", "style",
                     "noscript", "aside", "iframe"]):
        tag.decompose()

    # Remove common cookie/banner divs by class/id patterns
    for el in soup.find_all(True):
        attrs = " ".join(str(el.get("class", "")) + str(el.get("id", ""))).lower()
        if any(w in attrs for w in ["cookie", "gdpr", "banner", "popup",
                                     "modal", "overlay", "subscribe"]):
            el.decompose()

    text = soup.get_text(separator=" ", strip=True)
    # Collapse excessive whitespace
    import re
    text = re.sub(r"\s{3,}", "  ", text)
    return text[:config.CONTENT_MAX_CHARS]


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


# ---------------------------------------------------------------------------
# URL discovery
# ---------------------------------------------------------------------------

def _same_domain(url: str, base_url: str) -> bool:
    """Return True if url lives on the same domain as base_url."""
    try:
        return urlparse(url).netloc == urlparse(base_url).netloc
    except Exception:
        return False


async def _discover_links(context: BrowserContext, homepage_url: str,
                          html: str) -> list[str]:
    """
    Parse homepage HTML for links that look grant/news related and belong
    to the same domain. Returns de-duplicated absolute URLs.
    """
    soup = BeautifulSoup(html, "html.parser")
    seen: set[str] = set()
    found: list[str] = []

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        link_text = a.get_text(strip=True).lower()

        # Resolve relative URLs
        try:
            absolute = urljoin(homepage_url, href)
        except Exception:
            continue

        # Skip external links, fragments, mailto, tel
        if not _same_domain(absolute, homepage_url):
            continue
        if absolute.startswith(("mailto:", "tel:", "javascript:")):
            continue
        if "#" in absolute:
            absolute = absolute.split("#")[0]
        if not absolute or absolute in seen:
            continue

        # Keep only links whose URL path or visible text contains a keyword
        path_and_text = urlparse(absolute).path.lower() + " " + link_text
        if any(kw in path_and_text for kw in LINK_KEYWORDS):
            seen.add(absolute)
            found.append(absolute)

    return found


async def _probe_paths(context: BrowserContext, base_url: str) -> list[str]:
    """
    HTTP-GET each path in CRAWLER_PROBE_PATHS using Playwright's request API
    (no browser rendering — fast). Returns paths that returned HTTP 200.
    """
    parsed = urlparse(base_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    found: list[str] = []

    for path in config.CRAWLER_PROBE_PATHS:
        url = origin + path
        try:
            resp = await context.request.get(
                url,
                timeout=8_000,
                max_redirects=3,
            )
            if resp.status == 200:
                found.append(url)
        except Exception:
            continue

    return found


# ---------------------------------------------------------------------------
# Per-funder crawl
# ---------------------------------------------------------------------------

async def _crawl_url(page, url: str, funder_id: int) -> CrawlResult:
    """Navigate to a single URL and return a CrawlResult."""
    try:
        response = await page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=config.CRAWLER_PAGE_TIMEOUT_MS,
        )

        if response is None:
            return CrawlResult(funder_id=funder_id, url=url,
                               content_text="", content_hash="",
                               status="error", error_message="No response")

        if response.status == 403 or response.status == 429:
            return CrawlResult(funder_id=funder_id, url=url,
                               content_text="", content_hash="",
                               status="blocked",
                               error_message=f"HTTP {response.status}")

        if response.status >= 400:
            return CrawlResult(funder_id=funder_id, url=url,
                               content_text="", content_hash="",
                               status="error",
                               error_message=f"HTTP {response.status}")

        html = await page.content()
        text = _extract_text(html)
        return CrawlResult(
            funder_id=funder_id,
            url=url,
            content_text=text,
            content_hash=_hash(text),
            status="ok",
        )

    except Exception as e:
        msg = str(e)[:300]
        status = "blocked" if "403" in msg or "blocked" in msg.lower() else "error"
        return CrawlResult(funder_id=funder_id, url=url,
                           content_text="", content_hash="",
                           status=status, error_message=msg)


async def crawl_funder(
    funder: Funder,
    browser: Browser,
    semaphore: asyncio.Semaphore,
) -> list[CrawlResult]:
    """
    Crawl all relevant pages for a single funder.
    Steps:
      1. Load homepage, extract text
      2. Discover nav/content links that match grant/news keywords
      3. Probe fixed paths via HTTP HEAD (fast)
      4. Crawl discovered + probed URLs (up to MAX_PAGES_PER_FUNDER total)
    """
    async with semaphore:
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            ignore_https_errors=True,
        )
        page = await context.new_page()
        results: list[CrawlResult] = []

        try:
            # --- Step 1: Homepage ---
            homepage_result = await _crawl_url(page, funder.website_url, funder.id)
            results.append(homepage_result)

            if homepage_result.status != "ok":
                return results

            homepage_html = await page.content()

            # --- Step 2: Link discovery from homepage ---
            discovered = await _discover_links(
                context, funder.website_url, homepage_html
            )

            # --- Step 3: Probe fixed paths ---
            probed = await _probe_paths(context, funder.website_url)

            # --- Step 4: Combine, de-duplicate, limit, crawl ---
            all_urls = list(dict.fromkeys(discovered + probed))
            # Remove homepage (already crawled) and limit total
            extra_urls = [
                u for u in all_urls
                if u.rstrip("/") != funder.website_url.rstrip("/")
            ][:MAX_PAGES_PER_FUNDER - 1]

            for url in extra_urls:
                result = await _crawl_url(page, url, funder.id)
                results.append(result)

        except Exception as e:
            results.append(CrawlResult(
                funder_id=funder.id,
                url=funder.website_url,
                content_text="", content_hash="",
                status="error", error_message=str(e)[:300],
            ))
        finally:
            await page.close()
            await context.close()

        return results


# ---------------------------------------------------------------------------
# Async orchestration
# ---------------------------------------------------------------------------

async def run_crawls(funders: list[Funder]) -> list[list[CrawlResult]]:
    """Run all funder crawls concurrently, limited by CRAWLER_BATCH_SIZE."""
    semaphore = asyncio.Semaphore(config.CRAWLER_BATCH_SIZE)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        tasks = [crawl_funder(f, browser, semaphore) for f in funders]

        all_results = []
        # Process in chunks so we can print progress
        chunk_size = config.CRAWLER_BATCH_SIZE * 3
        for i in range(0, len(tasks), chunk_size):
            chunk = tasks[i:i + chunk_size]
            chunk_results = await asyncio.gather(*chunk, return_exceptions=True)
            all_results.extend(chunk_results)
            done = min(i + chunk_size, len(tasks))
            print(f"  Progress: {done}/{len(tasks)} funders crawled")

        await browser.close()

    return all_results


# ---------------------------------------------------------------------------
# Database persistence
# ---------------------------------------------------------------------------

def _load_last_hashes(session: Session, funder_ids: list[int]) -> dict[tuple, str]:
    """
    Return a dict of (funder_id, url) → most_recent_hash for quick
    change detection when saving results.
    """
    if not funder_ids:
        return {}

    # Subquery: for each (funder_id, url) pair, get the most recent snapshot id
    from sqlalchemy import func
    subq = (
        session.query(
            PageSnapshot.funder_id,
            PageSnapshot.url,
            func.max(PageSnapshot.id).label("max_id"),
        )
        .filter(PageSnapshot.funder_id.in_(funder_ids))
        .group_by(PageSnapshot.funder_id, PageSnapshot.url)
        .subquery()
    )
    rows = session.execute(
        select(PageSnapshot.funder_id, PageSnapshot.url, PageSnapshot.content_hash)
        .join(subq, PageSnapshot.id == subq.c.max_id)
    ).all()

    return {(r.funder_id, r.url): r.content_hash for r in rows}


def save_results(
    results: list[list[CrawlResult]],
    funders: list[Funder],
    session: Session,
) -> CrawlSummary:
    """
    Save all crawl results to page_snapshots and update funders.last_checked_at.
    Returns a summary dict for reporting.
    """
    funder_ids = [f.id for f in funders]
    funder_map = {f.id: f for f in funders}
    last_hashes = _load_last_hashes(session, funder_ids)
    now = datetime.now(timezone.utc)
    summary = CrawlSummary(total_funders=len(funders))

    for funder_results in results:
        if isinstance(funder_results, Exception):
            summary.errors += 1
            continue

        funder_id = funder_results[0].funder_id if funder_results else None
        if funder_id and funder_id in funder_map:
            funder_map[funder_id].last_checked_at = now

        for result in funder_results:
            summary.results.append(result)

            if result.status == "ok":
                summary.ok += 1
            elif result.status == "blocked":
                summary.blocked += 1
            else:
                summary.errors += 1

            prev_hash = last_hashes.get((result.funder_id, result.url))
            if prev_hash is None:
                summary.new_pages += 1
            elif prev_hash != result.content_hash and result.status == "ok":
                summary.changed += 1
            elif result.status == "ok":
                summary.unchanged += 1

            session.add(PageSnapshot(
                funder_id=result.funder_id,
                crawled_at=now,
                url=result.url,
                content_hash=result.content_hash or None,
                content_text=result.content_text or None,
                status=result.status,
                error_message=result.error_message,
            ))

    session.commit()
    return summary


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(limit: int | None = None, funder_id: int | None = None) -> CrawlSummary:
    engine = create_engine(config.DATABASE_URL)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        query = select(Funder).where(Funder.is_active == True)
        if funder_id:
            query = query.where(Funder.id == funder_id)
        if limit:
            query = query.limit(limit)

        funders = list(session.scalars(query).all())

    if not funders:
        print("No active funders found. Run the directory scraper first.")
        return CrawlSummary()

    print(f"Crawling {len(funders)} funder(s) with {config.CRAWLER_BATCH_SIZE} concurrent workers …")
    raw_results = asyncio.run(run_crawls(funders))

    with Session(engine) as session:
        # Re-attach funders to this session for update
        funders = list(session.scalars(
            select(Funder).where(Funder.id.in_([f.id for f in funders]))
        ).all())
        summary = save_results(raw_results, funders, session)

    print(f"\nCrawl complete:")
    print(f"  Funders crawled:   {summary.total_funders}")
    print(f"  Pages OK:          {summary.ok}")
    print(f"  Pages changed:     {summary.changed}  ← queued for AI review")
    print(f"  Pages unchanged:   {summary.unchanged}")
    print(f"  New pages (first): {summary.new_pages}")
    print(f"  Errors:            {summary.errors}")
    print(f"  Blocked:           {summary.blocked}")

    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Crawl active funder websites.")
    parser.add_argument("--limit", type=int, help="Only crawl N funders (for testing)")
    parser.add_argument("--funder-id", type=int, help="Crawl a single funder by DB id")
    args = parser.parse_args()

    try:
        main(limit=args.limit, funder_id=args.funder_id)
    except Exception as e:
        print(f"\nError: {e}", file=sys.stderr)
        sys.exit(1)
