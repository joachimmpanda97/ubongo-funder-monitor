"""
Tracking server — Flask app for email open and click tracking.

Endpoints:
  GET /track/open/<notification_id>/<recipient_b64>
      → logs open with recipient email, returns 1x1 pixel

  GET /track/click/<notification_id>/<opportunity_id>/<recipient_b64>
      → logs click with recipient email, redirects to opportunity URL

  GET /stats
      → dashboard showing opens and clicks with EAT timestamps
"""

import base64
from datetime import datetime, timezone, timedelta

from flask import Flask, redirect, request
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import config
from db.models import Base, EmailClick, EmailOpen, Opportunity

app = Flask(__name__)
engine = create_engine(config.DATABASE_URL)
Base.metadata.create_all(engine)

# East Africa Time (UTC+3)
EAT = timezone(timedelta(hours=3))

# 1x1 transparent GIF
PIXEL = base64.b64decode(
    "R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"
)


def _decode_email(b64: str) -> str:
    try:
        return base64.urlsafe_b64decode(b64.encode()).decode()
    except Exception:
        return "unknown"


def _now_eat() -> datetime:
    return datetime.now(timezone.utc).astimezone(EAT)


@app.route("/track/open/<int:notification_id>/<recipient_b64>")
def track_open(notification_id: int, recipient_b64: str):
    recipient = _decode_email(recipient_b64)
    with Session(engine) as session:
        session.add(EmailOpen(
            notification_id=notification_id,
            recipient_email=recipient,
            opened_at=datetime.now(timezone.utc),
            ip_address=request.remote_addr,
            user_agent=request.headers.get("User-Agent"),
        ))
        session.commit()

    return app.response_class(response=PIXEL, status=200, mimetype="image/gif")


@app.route("/track/click/<int:notification_id>/<int:opportunity_id>/<recipient_b64>")
def track_click(notification_id: int, opportunity_id: int, recipient_b64: str):
    recipient = _decode_email(recipient_b64)
    with Session(engine) as session:
        session.add(EmailClick(
            opportunity_id=opportunity_id,
            notification_id=notification_id,
            recipient_email=recipient,
            clicked_at=datetime.now(timezone.utc),
            ip_address=request.remote_addr,
            user_agent=request.headers.get("User-Agent"),
        ))
        session.commit()

        opp = session.get(Opportunity, opportunity_id)
        destination = opp.source_url if opp and opp.source_url else "https://www.segalfamilyfoundation.org"

    return redirect(destination, code=302)


@app.route("/stats")
def stats():
    with Session(engine) as session:
        opens = session.query(EmailOpen).order_by(EmailOpen.opened_at.desc()).all()
        clicks = session.query(EmailClick, Opportunity)\
            .join(Opportunity, EmailClick.opportunity_id == Opportunity.id)\
            .order_by(EmailClick.clicked_at.desc())\
            .all()

    def fmt(dt):
        if dt is None:
            return "-"
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(EAT).strftime("%Y-%m-%d %H:%M EAT")

    open_rows = "".join(
        f"<tr><td>{o.recipient_email or '—'}</td><td>{fmt(o.opened_at)}</td></tr>"
        for o in opens
    )

    click_rows = "".join(
        f"<tr><td>{c.recipient_email or '—'}</td><td>{o.title}</td><td>{fmt(c.clicked_at)}</td></tr>"
        for c, o in clicks
    )

    return f"""<!DOCTYPE html>
<html>
<head>
  <title>Ubongo Funder Monitor — Email Stats</title>
  <style>
    body {{ font-family: sans-serif; padding: 32px; background: #f9fafb; }}
    h1 {{ color: #16a34a; }}
    h2 {{ margin-top: 32px; color: #111827; }}
    table {{ border-collapse: collapse; width: 100%; background: #fff;
             box-shadow: 0 1px 3px rgba(0,0,0,0.1); border-radius: 8px; overflow: hidden; }}
    th {{ background: #16a34a; color: #fff; padding: 10px 16px; text-align: left; }}
    td {{ padding: 10px 16px; border-bottom: 1px solid #e5e7eb; }}
    tr:last-child td {{ border-bottom: none; }}
    .summary {{ display: flex; gap: 24px; margin-bottom: 24px; }}
    .card {{ background: #fff; border-radius: 8px; padding: 20px 28px;
             box-shadow: 0 1px 3px rgba(0,0,0,0.1); }}
    .card .num {{ font-size: 36px; font-weight: bold; color: #16a34a; }}
    .card .label {{ font-size: 13px; color: #6b7280; }}
  </style>
</head>
<body>
  <h1>Ubongo Funder Monitor — Email Stats</h1>
  <div class="summary">
    <div class="card"><div class="num">{len(opens)}</div><div class="label">Total Opens</div></div>
    <div class="card"><div class="num">{len(clicks)}</div><div class="label">Total Clicks</div></div>
  </div>

  <h2>Opens</h2>
  <table>
    <tr><th>Email Address</th><th>Opened At (EAT)</th></tr>
    {open_rows or '<tr><td colspan="2">No opens yet</td></tr>'}
  </table>

  <h2>Clicks</h2>
  <table>
    <tr><th>Email Address</th><th>Opportunity</th><th>Clicked At (EAT)</th></tr>
    {click_rows or '<tr><td colspan="3">No clicks yet</td></tr>'}
  </table>
</body>
</html>"""


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
