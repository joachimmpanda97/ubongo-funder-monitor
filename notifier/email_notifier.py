"""
Phase 5 — Email Notifier
=========================
Fetches all unnotified opportunities from the database, builds an HTML +
plain-text email digest, and sends it via AWS SES.

Two email types:
  - Opportunities digest  → when new opportunities were found this week
  - All-clear status      → when no opportunities found (confirms system ran)

After a successful send:
  - Marks all included opportunities as notified=True
  - Inserts a row in notification_log

Usage (called by the weekly scheduler, or standalone for testing):
    python -m notifier.email_notifier --dry-run   # preview without sending
"""

import argparse
import sys
from datetime import date, datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import boto3
from sqlalchemy import select
from sqlalchemy.orm import Session

import config
from db.models import Funder, NotificationLog, Opportunity


# ---------------------------------------------------------------------------
# Email content builders
# ---------------------------------------------------------------------------

def _format_deadline(d: date | None) -> str:
    if d is None:
        return "Not specified"
    return d.strftime("%B %d, %Y")


def _build_html(opportunities: list[tuple[Opportunity, Funder]], week_str: str) -> str:
    count = len(opportunities)
    subject_line = f"{count} New Funding {'Opportunity' if count == 1 else 'Opportunities'} Found"

    rows_html = ""
    for i, (opp, funder) in enumerate(opportunities, 1):
        deadline_str = _format_deadline(opp.deadline)
        url = opp.source_url or funder.website_url or ""
        rows_html += f"""
        <tr>
          <td style="padding:16px 0; border-bottom:1px solid #e5e7eb; vertical-align:top;">
            <p style="margin:0 0 4px; font-size:11px; color:#6b7280; text-transform:uppercase;
                      letter-spacing:0.05em;">#{i} · {funder.name}</p>
            <h2 style="margin:0 0 8px; font-size:17px; color:#111827;">{opp.title}</h2>
            <p style="margin:0 0 10px; font-size:14px; color:#374151; line-height:1.6;">
              {opp.summary}
            </p>
            <table style="margin-bottom:10px;">
              <tr>
                <td style="font-size:12px; color:#6b7280; padding-right:16px;">
                  <strong>Deadline:</strong> {deadline_str}
                </td>
              </tr>
            </table>
            <a href="{url}"
               style="display:inline-block; background:#16a34a; color:#ffffff;
                      font-size:13px; font-weight:600; padding:8px 16px;
                      border-radius:6px; text-decoration:none;">
              View Opportunity →
            </a>
          </td>
        </tr>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0; padding:0; background:#f9fafb; font-family:-apple-system,BlinkMacSystemFont,
             'Segoe UI',Roboto,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0">
    <tr>
      <td align="center" style="padding:32px 16px;">
        <table width="600" cellpadding="0" cellspacing="0"
               style="background:#ffffff; border-radius:8px;
                      box-shadow:0 1px 3px rgba(0,0,0,0.1);">

          <!-- Header -->
          <tr>
            <td style="background:#16a34a; padding:24px 32px; border-radius:8px 8px 0 0;">
              <p style="margin:0; font-size:12px; color:#bbf7d0; letter-spacing:0.1em;
                        text-transform:uppercase;">Ubongo Funder Monitor</p>
              <h1 style="margin:8px 0 0; font-size:22px; color:#ffffff;">
                {subject_line}
              </h1>
              <p style="margin:6px 0 0; font-size:13px; color:#dcfce7;">Week of {week_str}</p>
            </td>
          </tr>

          <!-- Body -->
          <tr>
            <td style="padding:24px 32px;">
              <table width="100%" cellpadding="0" cellspacing="0">
                {rows_html}
              </table>
            </td>
          </tr>

          <!-- Footer -->
          <tr>
            <td style="padding:16px 32px 24px; border-top:1px solid #f3f4f6;">
              <p style="margin:0; font-size:12px; color:#9ca3af;">
                Sent automatically by the Ubongo Funder Monitor.
                To manage recipients, update the TEAM_EMAILS environment variable.
              </p>
            </td>
          </tr>

        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""


def _build_plain(opportunities: list[tuple[Opportunity, Funder]], week_str: str) -> str:
    count = len(opportunities)
    lines = [
        f"UBONGO FUNDER MONITOR — {count} New Funding "
        f"{'Opportunity' if count == 1 else 'Opportunities'}",
        f"Week of {week_str}",
        "=" * 60,
        "",
    ]
    for i, (opp, funder) in enumerate(opportunities, 1):
        lines += [
            f"{i}. {opp.title}",
            f"   Funder:   {funder.name}",
            f"   Deadline: {_format_deadline(opp.deadline)}",
            f"   Summary:  {opp.summary}",
            f"   Link:     {opp.source_url or funder.website_url or ''}",
            "",
        ]
    lines += [
        "-" * 60,
        "Sent by Ubongo Funder Monitor.",
    ]
    return "\n".join(lines)


def _build_all_clear_html(week_str: str, funders_checked: int) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"></head>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
             background:#f9fafb; margin:0; padding:32px 16px;">
  <table width="560" align="center" style="background:#ffffff; border-radius:8px;
         box-shadow:0 1px 3px rgba(0,0,0,0.1); padding:32px;">
    <tr>
      <td>
        <p style="margin:0 0 4px; font-size:11px; color:#6b7280; text-transform:uppercase;">
          Ubongo Funder Monitor · Week of {week_str}
        </p>
        <h2 style="margin:0 0 16px; color:#111827;">✓ Weekly Check Complete</h2>
        <p style="color:#374151; line-height:1.6;">
          No new funding opportunities were detected this week across
          <strong>{funders_checked} funder websites</strong>.
        </p>
        <p style="color:#6b7280; font-size:13px; margin-top:24px;">
          — Ubongo Funder Monitor
        </p>
      </td>
    </tr>
  </table>
</body>
</html>"""


# ---------------------------------------------------------------------------
# SES sending
# ---------------------------------------------------------------------------

def _send_via_ses(subject: str, html_body: str, plain_body: str) -> str:
    """Send email via AWS SES. Returns the SES MessageId."""
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = config.SES_SENDER_EMAIL
    msg["To"] = ", ".join(config.TEAM_EMAILS)
    msg.attach(MIMEText(plain_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    ses = boto3.client(
        "ses",
        region_name=config.AWS_REGION,
        aws_access_key_id=config.AWS_ACCESS_KEY_ID,
        aws_secret_access_key=config.AWS_SECRET_ACCESS_KEY,
    )

    response = ses.send_raw_email(
        Source=config.SES_SENDER_EMAIL,
        Destinations=config.TEAM_EMAILS,
        RawMessage={"Data": msg.as_string()},
    )
    return response["MessageId"]


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def send_digest(session: Session, dry_run: bool = False) -> dict:
    """
    Fetch unnotified opportunities, send the digest, update DB.
    Returns a result dict with keys: sent, opportunity_count, message_id.
    """
    week_str = date.today().strftime("%B %d, %Y")

    # Load unnotified opportunities with their funder
    rows = session.execute(
        select(Opportunity, Funder)
        .join(Funder, Opportunity.funder_id == Funder.id)
        .where(Opportunity.notified == False)
        .order_by(Opportunity.detected_at.desc())
    ).all()

    opportunities = [(opp, funder) for opp, funder in rows]
    opp_ids = [opp.id for opp, _ in opportunities]

    if opportunities:
        subject = (
            f"[Ubongo] {len(opportunities)} New Funding "
            f"{'Opportunity' if len(opportunities) == 1 else 'Opportunities'} "
            f"— Week of {week_str}"
        )
        html = _build_html(opportunities, week_str)
        plain = _build_plain(opportunities, week_str)
    else:
        # Count funders checked this week for the all-clear email
        from sqlalchemy import func
        from db.models import PageSnapshot
        from datetime import timedelta
        since = datetime.now(timezone.utc) - timedelta(days=8)
        funders_checked = session.scalar(
            select(func.count(func.distinct(PageSnapshot.funder_id)))
            .where(PageSnapshot.crawled_at >= since)
        ) or 0

        subject = f"[Ubongo] All Clear — No New Opportunities — Week of {week_str}"
        html = _build_all_clear_html(week_str, funders_checked)
        plain = f"Ubongo Funder Monitor — No new opportunities this week ({week_str})."

    if dry_run:
        print(f"\n[dry-run] Would send to: {', '.join(config.TEAM_EMAILS)}")
        print(f"[dry-run] Subject: {subject}")
        print(f"[dry-run] Opportunities: {len(opportunities)}")
        print("\n--- Plain text preview ---")
        print(plain[:800])
        return {"sent": False, "opportunity_count": len(opportunities), "message_id": None}

    # Send
    message_id = _send_via_ses(subject, html, plain)

    # Mark opportunities as notified
    for opp, _ in opportunities:
        opp.notified = True

    # Log the send
    session.add(NotificationLog(
        sent_at=datetime.now(timezone.utc),
        recipient_emails=config.TEAM_EMAILS,
        opportunity_ids=opp_ids,
    ))
    session.commit()

    return {
        "sent": True,
        "opportunity_count": len(opportunities),
        "message_id": message_id,
    }


# ---------------------------------------------------------------------------
# Standalone CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Send the weekly email digest.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview the email without sending")
    args = parser.parse_args()

    from sqlalchemy import create_engine
    from db.models import Base

    engine = create_engine(config.DATABASE_URL)
    Base.metadata.create_all(engine)

    try:
        with Session(engine) as session:
            result = send_digest(session, dry_run=args.dry_run)

        if result["sent"]:
            print(f"Email sent. Opportunities: {result['opportunity_count']}. "
                  f"SES MessageId: {result['message_id']}")
        elif not args.dry_run:
            print("Email not sent (no change).")
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
