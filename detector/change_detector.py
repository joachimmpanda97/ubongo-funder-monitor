"""
Phase 4a — Change Detector
===========================
Queries the database to find page snapshots that changed since the previous
crawl. These are the pages that get passed to the Claude AI filter.

Logic per (funder_id, url) pair:
  - If only one snapshot exists (first ever crawl) → skip.
    We need a baseline before we can detect change.
  - If two or more snapshots exist and the latest hash ≠ previous hash
    AND latest status = 'ok' → flag as changed.
  - If latest status = 'error' or 'blocked' → skip (nothing to analyse).
"""

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from db.models import Funder, PageSnapshot


@dataclass
class ChangedPage:
    funder: Funder
    snapshot: PageSnapshot     # the NEW (latest) snapshot
    prev_snapshot: PageSnapshot  # the previous snapshot for diffing


def find_changed_pages(session: Session) -> list[ChangedPage]:
    """
    Return all pages whose content changed between the two most recent
    crawl snapshots.

    Algorithm:
      1. For every (funder_id, url) pair, find the two most recent
         snapshot IDs (ranked by id DESC).
      2. Keep pairs where rank-1 status = 'ok' and
         rank-1 hash ≠ rank-2 hash.
    """

    # Step 1: rank snapshots per (funder_id, url), newest first
    from sqlalchemy import Integer, Text
    from sqlalchemy.orm import aliased

    S1 = aliased(PageSnapshot, name="s1")   # latest snapshot
    S2 = aliased(PageSnapshot, name="s2")   # previous snapshot

    # Subquery: max id (latest) per (funder_id, url)
    latest_sq = (
        session.query(
            PageSnapshot.funder_id,
            PageSnapshot.url,
            func.max(PageSnapshot.id).label("latest_id"),
        )
        .group_by(PageSnapshot.funder_id, PageSnapshot.url)
        .subquery()
    )

    # Subquery: second-highest id per (funder_id, url)
    prev_sq = (
        session.query(
            PageSnapshot.funder_id,
            PageSnapshot.url,
            func.max(PageSnapshot.id).label("prev_id"),
        )
        .join(
            latest_sq,
            (PageSnapshot.funder_id == latest_sq.c.funder_id)
            & (PageSnapshot.url == latest_sq.c.url)
            & (PageSnapshot.id < latest_sq.c.latest_id),
        )
        .group_by(PageSnapshot.funder_id, PageSnapshot.url)
        .subquery()
    )

    # Join latest and previous, keep changed pages
    rows = (
        session.query(S1, S2)
        .join(latest_sq, S1.id == latest_sq.c.latest_id)
        .join(
            prev_sq,
            (S1.funder_id == prev_sq.c.funder_id)
            & (S1.url == prev_sq.c.url),
        )
        .join(S2, S2.id == prev_sq.c.prev_id)
        .filter(
            S1.status == "ok",
            S1.content_hash != S2.content_hash,
        )
        .all()
    )

    if not rows:
        return []

    # Fetch corresponding Funder objects
    funder_ids = list({s1.funder_id for s1, _ in rows})
    funders = {
        f.id: f
        for f in session.scalars(
            select(Funder).where(Funder.id.in_(funder_ids))
        ).all()
    }

    return [
        ChangedPage(
            funder=funders[s1.funder_id],
            snapshot=s1,
            prev_snapshot=s2,
        )
        for s1, s2 in rows
        if s1.funder_id in funders
    ]


def summary(changed: list[ChangedPage]) -> str:
    """Return a one-line summary string for logging."""
    funders = len({c.funder.id for c in changed})
    return f"{len(changed)} changed page(s) across {funders} funder(s)"
