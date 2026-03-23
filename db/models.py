from datetime import datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Text,
    create_engine,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Funder(Base):
    __tablename__ = "funders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    website_url: Mapped[str] = mapped_column(Text, nullable=False)
    focus_areas: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    geography: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    # True = crawl weekly; False = skip (failed to filter in, or manually disabled)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    snapshots: Mapped[list["PageSnapshot"]] = relationship(back_populates="funder")
    opportunities: Mapped[list["Opportunity"]] = relationship(back_populates="funder")

    def __repr__(self) -> str:
        return f"<Funder id={self.id} name={self.name!r} active={self.is_active}>"


class PageSnapshot(Base):
    __tablename__ = "page_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    funder_id: Mapped[int] = mapped_column(Integer, ForeignKey("funders.id"), nullable=False)
    crawled_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    # ok | error | blocked
    status: Mapped[str] = mapped_column(Text, nullable=False, default="ok")
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    funder: Mapped["Funder"] = relationship(back_populates="snapshots")
    opportunities: Mapped[list["Opportunity"]] = relationship(back_populates="snapshot")

    def __repr__(self) -> str:
        return f"<PageSnapshot id={self.id} funder_id={self.funder_id} status={self.status!r}>"


class Opportunity(Base):
    __tablename__ = "opportunities"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    funder_id: Mapped[int] = mapped_column(Integer, ForeignKey("funders.id"), nullable=False)
    snapshot_id: Mapped[int] = mapped_column(Integer, ForeignKey("page_snapshots.id"), nullable=False)
    detected_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    deadline: Mapped[datetime | None] = mapped_column(Date, nullable=True)
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    # False until included in a sent email digest
    notified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    funder: Mapped["Funder"] = relationship(back_populates="opportunities")
    snapshot: Mapped["PageSnapshot"] = relationship(back_populates="opportunities")

    def __repr__(self) -> str:
        return f"<Opportunity id={self.id} funder_id={self.funder_id} notified={self.notified}>"


class NotificationLog(Base):
    __tablename__ = "notification_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sent_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    recipient_emails: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    opportunity_ids: Mapped[list[int]] = mapped_column(ARRAY(Integer), nullable=False)

    opens: Mapped[list["EmailOpen"]] = relationship(back_populates="notification")

    def __repr__(self) -> str:
        return f"<NotificationLog id={self.id} sent_at={self.sent_at} opportunities={self.opportunity_ids}>"


class EmailOpen(Base):
    __tablename__ = "email_opens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    notification_id: Mapped[int] = mapped_column(Integer, ForeignKey("notification_log.id"), nullable=False)
    recipient_email: Mapped[str | None] = mapped_column(Text, nullable=True)
    opened_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    ip_address: Mapped[str | None] = mapped_column(Text, nullable=True)
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)

    notification: Mapped["NotificationLog"] = relationship(back_populates="opens")

    def __repr__(self) -> str:
        return f"<EmailOpen id={self.id} recipient={self.recipient_email} opened_at={self.opened_at}>"


class EmailClick(Base):
    __tablename__ = "email_clicks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    opportunity_id: Mapped[int] = mapped_column(Integer, ForeignKey("opportunities.id"), nullable=False)
    notification_id: Mapped[int] = mapped_column(Integer, ForeignKey("notification_log.id"), nullable=False)
    recipient_email: Mapped[str | None] = mapped_column(Text, nullable=True)
    clicked_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    ip_address: Mapped[str | None] = mapped_column(Text, nullable=True)
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)

    def __repr__(self) -> str:
        return f"<EmailClick id={self.id} recipient={self.recipient_email} opportunity_id={self.opportunity_id}>"
