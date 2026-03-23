"""
Tracking server — Flask app for email open and click tracking.

Endpoints:
  GET /track/open/<notification_id>   → logs open, returns 1x1 pixel
  GET /track/click/<notification_id>/<opportunity_id> → logs click, redirects
  GET /stats                          → simple stats page

Run via systemd (see deploy/ubongo-tracker.service).
"""

import base64
from datetime import datetime

from flask import Flask, redirect, request
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import config
from db.models import Base, EmailClick, EmailOpen, Opportunity

app = Flask(__name__)
engine = create_engine(config.DATABASE_URL)
Base.metadata.create_all(engine)

# 1x1 transparent GIF
PIXEL = base64.b64decode(
    "R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"
)


@app.route("/track/open/<int:notification_id>")
def track_open(notification_id: int):
    with Session(engine) as session:
        session.add(EmailOpen(
            notification_id=notification_id,
            opened_at=datetime.utcnow(),
            ip_address=request.remote_addr,
            user_agent=request.headers.get("User-Agent"),
        ))
        session.commit()

    return app.response_class(
        response=PIXEL,
        status=200,
        mimetype="image/gif",
    )


@app.route("/track/click/<int:notification_id>/<int:opportunity_id>")
def track_click(notification_id: int, opportunity_id: int):
    with Session(engine) as session:
        session.add(EmailClick(
            opportunity_id=opportunity_id,
            notification_id=notification_id,
            clicked_at=datetime.utcnow(),
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
        opens = session.query(EmailOpen).count()
        clicks = session.query(EmailClick).count()

        recent_clicks = (
            session.query(EmailClick, Opportunity)
            .join(Opportunity, EmailClick.opportunity_id == Opportunity.id)
            .order_by(EmailClick.clicked_at.desc())
            .limit(10)
            .all()
        )

    rows = "".join(
        f"<tr><td>{c.clicked_at:%Y-%m-%d %H:%M}</td><td>{o.title}</td><td>{c.ip_address}</td></tr>"
        for c, o in recent_clicks
    )

    return f"""
    <html><body style="font-family:sans-serif;padding:32px;">
    <h2>Ubongo Funder Monitor — Email Stats</h2>
    <p><strong>Total opens:</strong> {opens}</p>
    <p><strong>Total clicks:</strong> {clicks}</p>
    <h3>Recent clicks</h3>
    <table border="1" cellpadding="6" cellspacing="0">
      <tr><th>Time</th><th>Opportunity</th><th>IP</th></tr>
      {rows}
    </table>
    </body></html>
    """


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
