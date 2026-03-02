"""
Phase 6 — Weekly Pipeline Orchestrator
========================================
Two cron jobs, two modes:

  --mode crawl   Sunday 23:00 UTC (Monday 02:00 EAT)
                 Crawls all funder sites, detects changes, runs Claude AI
                 filter, and saves opportunities to the database.
                 Does NOT send email yet.

  --mode email   Monday 05:00 UTC (Monday 08:00 EAT)
                 Picks up all unnotified opportunities saved by the crawl
                 job and sends the email digest to the team.

  (no flag)      Runs both steps back-to-back — useful for a manual
                 full run or initial test.

Usage:
    python -m scheduler.weekly_run --mode crawl
    python -m scheduler.weekly_run --mode email
    python -m scheduler.weekly_run               # both
"""

import argparse
import logging
import sys
import time
from datetime import datetime, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import config
from db.models import Base
from detector.change_detector import find_changed_pages, summary as change_summary
from detector.opportunity_filter import print_summary as print_filter_summary, run_filter
from notifier.email_notifier import send_digest
from scraper.site_crawler import main as run_crawler

# ---------------------------------------------------------------------------
# Logging — stdout so cron captures it to the log file
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Step runners
# ---------------------------------------------------------------------------

def step_crawl(engine) -> int:
    """Crawl → detect changes → run Claude filter. Returns opportunity count."""
    # 1. Crawl
    logger.info("── Step 1/3  Crawling funder websites …")
    t0 = time.time()
    crawl_summary = run_crawler()
    logger.info(
        "── Step 1/3  Done in %.0fs — %d pages OK, %d changed, %d errors",
        time.time() - t0,
        crawl_summary.ok,
        crawl_summary.changed,
        crawl_summary.errors,
    )

    # 2. Change detection
    logger.info("── Step 2/3  Detecting changed pages …")
    with Session(engine) as session:
        changed_pages = find_changed_pages(session)
    logger.info("── Step 2/3  %s", change_summary(changed_pages))

    # 3. Claude AI filter
    logger.info("── Step 3/3  Running Claude AI filter on %d page(s) …", len(changed_pages))
    t0 = time.time()

    with Session(engine) as session:
        from sqlalchemy import select
        from db.models import Funder, PageSnapshot
        from detector.change_detector import ChangedPage

        reattached = []
        for cp in changed_pages:
            funder = session.get(Funder, cp.funder.id)
            snapshot = session.get(PageSnapshot, cp.snapshot.id)
            prev = session.get(PageSnapshot, cp.prev_snapshot.id)
            if funder and snapshot and prev:
                reattached.append(ChangedPage(
                    funder=funder, snapshot=snapshot, prev_snapshot=prev
                ))

        filter_summary = run_filter(reattached, session)

    logger.info(
        "── Step 3/3  Done in %.0fs — %d opportunities saved to DB",
        time.time() - t0,
        filter_summary.opportunities_found,
    )
    print_filter_summary(filter_summary)
    return filter_summary.opportunities_found


def step_email(engine) -> None:
    """Send email digest of all unnotified opportunities."""
    logger.info("── Sending Monday 08:00 EAT email digest …")
    with Session(engine) as session:
        result = send_digest(session)

    if result["sent"]:
        logger.info(
            "── Email sent to: %s | Opportunities: %d | SES MessageId: %s",
            ", ".join(config.TEAM_EMAILS),
            result["opportunity_count"],
            result["message_id"],
        )
    else:
        logger.info("── Email not sent (no unnotified opportunities).")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(mode: str) -> None:
    started_at = datetime.now(timezone.utc)
    label = {"crawl": "CRAWL", "email": "EMAIL", "both": "FULL RUN"}[mode]

    logger.info("=" * 60)
    logger.info("Ubongo Funder Monitor — %s started", label)
    logger.info("=" * 60)

    engine = create_engine(config.DATABASE_URL)
    Base.metadata.create_all(engine)

    if mode in ("crawl", "both"):
        step_crawl(engine)

    if mode in ("email", "both"):
        step_email(engine)

    elapsed = (datetime.now(timezone.utc) - started_at).total_seconds()
    logger.info("=" * 60)
    logger.info("%s complete in %.0f seconds.", label, elapsed)
    logger.info("=" * 60)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Ubongo funder monitor pipeline.")
    parser.add_argument(
        "--mode",
        choices=["crawl", "email", "both"],
        default="both",
        help=(
            "crawl = Sunday job (crawl + detect + AI filter); "
            "email = Monday job (send digest); "
            "both = full run (default)"
        ),
    )
    args = parser.parse_args()

    try:
        run(mode=args.mode)
    except Exception as e:
        logger.exception("Pipeline failed: %s", e)
        sys.exit(1)
