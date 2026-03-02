"""
Phase 4b — Opportunity Filter (Claude AI Layer)
================================================
For each changed page, calls the Claude API to determine whether the new
content contains a real, actionable funding opportunity.

Flow:
  1. Call claude-haiku (fast, cheap) with a structured JSON prompt.
  2. If confidence = 'low', re-call with claude-sonnet for a second opinion.
  3. If is_opportunity = true (and confidence != 'low' after escalation),
     insert an Opportunity row and mark it notified=False.
  4. Return a FilterSummary with counts.

Cost estimate (haiku): ~$0.001 per page → 150 changed pages/week ≈ $0.15/week.
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import date

import anthropic
from sqlalchemy.orm import Session

import config
from db.models import Opportunity
from detector.change_detector import ChangedPage

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are an expert at identifying funding opportunities for \
African education nonprofits.
You will be given the text content of a funder's website page.
Your job is to determine whether the page contains a specific, actionable \
funding opportunity that is currently open or opening soon.

Respond ONLY with a valid JSON object — no prose, no markdown fences."""

USER_PROMPT_TEMPLATE = """\
Funder: {funder_name}
URL: {url}

Page content:
---
{content_text}
---

Answer with this exact JSON structure:
{{
  "is_opportunity": true or false,
  "confidence": "high" or "medium" or "low",
  "title": "short title of the opportunity, or null",
  "summary": "2-3 sentence summary covering what the grant is for, who can \
apply, and the deadline if known — or null",
  "deadline": "YYYY-MM-DD or null",
  "direct_url": "direct URL to the opportunity if different from the page \
URL above, or null"
}}

Return is_opportunity: true ONLY if there is a specific, actionable grant, \
RFP, or funding call that is currently open or opening soon.
General descriptions of what a funder supports do NOT count.
A deadline in the past means the opportunity is closed — return false."""


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class FilterResult:
    changed_page: ChangedPage
    is_opportunity: bool
    confidence: str          # high | medium | low
    title: str | None
    summary: str | None
    deadline: date | None
    direct_url: str | None
    model_used: str
    raw_response: str


@dataclass
class FilterSummary:
    total_analysed: int = 0
    opportunities_found: int = 0
    skipped_low_confidence: int = 0
    api_errors: int = 0
    results: list[FilterResult] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Claude API call
# ---------------------------------------------------------------------------

def _call_claude(
    client: anthropic.Anthropic,
    funder_name: str,
    url: str,
    content_text: str,
    model: str,
) -> dict:
    """
    Call the Claude API and return the parsed JSON response dict.
    Raises ValueError if the response is not valid JSON.
    """
    user_message = USER_PROMPT_TEMPLATE.format(
        funder_name=funder_name,
        url=url,
        content_text=content_text[:config.CONTENT_MAX_CHARS],
    )

    message = client.messages.create(
        model=model,
        max_tokens=512,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
    )

    raw = message.content[0].text.strip()

    # Strip accidental markdown fences if model adds them
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()

    return json.loads(raw), raw


def _parse_deadline(raw: str | None) -> date | None:
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Main filter logic
# ---------------------------------------------------------------------------

def analyse_page(
    client: anthropic.Anthropic,
    changed: ChangedPage,
) -> FilterResult:
    """
    Run one changed page through Claude.
    Escalates to the careful model if haiku returns low confidence.
    """
    funder = changed.funder
    snapshot = changed.snapshot

    # First pass — fast model
    try:
        parsed, raw = _call_claude(
            client,
            funder_name=funder.name,
            url=snapshot.url,
            content_text=snapshot.content_text or "",
            model=config.CLAUDE_FAST_MODEL,
        )
        model_used = config.CLAUDE_FAST_MODEL
    except Exception as e:
        logger.warning("Claude API error for %s: %s", funder.name, e)
        raise

    # Escalate if haiku is unsure
    if parsed.get("confidence") == "low":
        try:
            parsed, raw = _call_claude(
                client,
                funder_name=funder.name,
                url=snapshot.url,
                content_text=snapshot.content_text or "",
                model=config.CLAUDE_CAREFUL_MODEL,
            )
            model_used = config.CLAUDE_CAREFUL_MODEL
        except Exception as e:
            logger.warning("Escalation API error for %s: %s", funder.name, e)
            # Fall back to haiku result

    return FilterResult(
        changed_page=changed,
        is_opportunity=bool(parsed.get("is_opportunity", False)),
        confidence=parsed.get("confidence", "low"),
        title=parsed.get("title"),
        summary=parsed.get("summary"),
        deadline=_parse_deadline(parsed.get("deadline")),
        direct_url=parsed.get("direct_url"),
        model_used=model_used,
        raw_response=raw,
    )


def run_filter(
    changed_pages: list[ChangedPage],
    session: Session,
) -> FilterSummary:
    """
    Analyse all changed pages, save confirmed opportunities to the DB.
    Returns a FilterSummary for reporting.
    """
    if not changed_pages:
        return FilterSummary()

    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    summary = FilterSummary(total_analysed=len(changed_pages))

    for i, changed in enumerate(changed_pages, 1):
        logger.info(
            "[%d/%d] Analysing %s — %s",
            i, len(changed_pages),
            changed.funder.name,
            changed.snapshot.url,
        )

        try:
            result = analyse_page(client, changed)
        except Exception as e:
            logger.error("Failed to analyse %s: %s", changed.funder.name, e)
            summary.api_errors += 1
            continue

        summary.results.append(result)

        # Skip if Claude is still unsure after escalation
        if result.confidence == "low":
            summary.skipped_low_confidence += 1
            continue

        if result.is_opportunity:
            session.add(Opportunity(
                funder_id=changed.funder.id,
                snapshot_id=changed.snapshot.id,
                title=result.title or f"Opportunity at {changed.funder.name}",
                summary=result.summary or "",
                deadline=result.deadline,
                source_url=result.direct_url or changed.snapshot.url,
                notified=False,
            ))
            summary.opportunities_found += 1

    session.commit()
    return summary


def print_summary(s: FilterSummary) -> None:
    print(f"\nAI filter complete:")
    print(f"  Pages analysed:        {s.total_analysed}")
    print(f"  Opportunities found:   {s.opportunities_found}  ← will be emailed")
    print(f"  Low confidence (skip): {s.skipped_low_confidence}")
    print(f"  API errors:            {s.api_errors}")
