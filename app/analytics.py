"""Database-backed transaction analytics, independent of Flask routes."""

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import Transaction

CENT = Decimal("0.01")
ZERO_MONEY = Decimal("0.00")
DEMO_CUTOFF_UTC = datetime(2026, 10, 19, tzinfo=timezone.utc)


@dataclass(frozen=True)
class DailyVolume:
    """Event count for one UTC calendar day."""

    day: date
    transaction_count: int


@dataclass(frozen=True)
class AnalyticsSummary:
    """Transaction event and successful USD amount metrics for a selected window."""

    currency: str
    total_transaction_count: int
    successful_transaction_count: int
    successful_transaction_value: Decimal
    average_successful_transaction_amount: Decimal | None
    counts_by_status: dict[str, int]
    counts_by_type: dict[str, int]
    counts_by_channel: dict[str, int]
    successful_deposit_total: Decimal
    successful_withdrawal_total: Decimal
    daily_volume: list[DailyVolume]


def _utc_naive(value: datetime | None, name: str) -> datetime | None:
    """Convert an aware datetime to UTC for the database's UTC DATETIME values."""
    if value is None:
        return None
    if not isinstance(value, datetime):
        raise TypeError(f"{name} must be a datetime or None.")
    if value.utcoffset() is None:
        raise ValueError(f"{name} must include a timezone.")
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def transaction_cutoff_filter():
    """Return the shared exclusive UTC cutoff predicate for dashboard queries."""
    return Transaction.occurred_at < _utc_naive(DEMO_CUTOFF_UTC, "DEMO_CUTOFF_UTC")


def summarize_transactions(
    session: Session,
    *,
    start_at: datetime | None = None,
    end_at: datetime | None = None,
) -> AnalyticsSummary:
    """Calculate event counts and successful USD totals for a half-open UTC window.

    `start_at` is inclusive and `end_at` is exclusive. Both may be omitted for
    the full demonstration period, or supplied independently to create an
    open-ended window. Results always stop before the demonstration cutoff.
    Any supplied boundary must be timezone-aware and is normalized to UTC.
    """
    start_utc = _utc_naive(start_at, "start_at")
    end_utc = _utc_naive(end_at, "end_at")
    if start_utc is not None and end_utc is not None and start_utc >= end_utc:
        raise ValueError("start_at must be earlier than end_at.")

    filters = [transaction_cutoff_filter()]
    if start_utc is not None:
        filters.append(Transaction.occurred_at >= start_utc)
    if end_utc is not None:
        filters.append(Transaction.occurred_at < end_utc)

    currencies = set(session.scalars(
        select(Transaction.currency).where(*filters).distinct()
    ).all())
    unsupported_currencies = currencies - {"USD"}
    if unsupported_currencies:
        found = ", ".join(sorted(unsupported_currencies))
        raise ValueError(f"Analytics currently supports USD only; found: {found}.")

    total_count = session.scalar(
        select(func.count(Transaction.id)).where(*filters)
    ) or 0

    status_rows = session.execute(
        select(Transaction.status, func.count(Transaction.id))
        .where(*filters)
        .group_by(Transaction.status)
        .order_by(Transaction.status)
    )
    counts_by_status = {status: count for status, count in status_rows}

    type_rows = session.execute(
        select(Transaction.transaction_type, func.count(Transaction.id))
        .where(*filters)
        .group_by(Transaction.transaction_type)
        .order_by(Transaction.transaction_type)
    )
    counts_by_type = {transaction_type: count for transaction_type, count in type_rows}

    channel_rows = session.execute(
        select(Transaction.channel, func.count(Transaction.id))
        .where(*filters)
        .group_by(Transaction.channel)
    )
    channel_counts = {channel or "UNKNOWN": count for channel, count in channel_rows}
    counts_by_channel = {
        channel: channel_counts.get(channel, 0)
        for channel in ("UPI", "ATM", "BRANCH", "UNKNOWN")
    }

    successful_rows = session.execute(
        select(Transaction.amount, Transaction.transaction_type)
        .where(*filters, Transaction.status == "successful")
        .order_by(Transaction.id)
    )
    successful_count = 0
    successful_value = ZERO_MONEY
    deposit_total = ZERO_MONEY
    withdrawal_total = ZERO_MONEY
    for amount, transaction_type in successful_rows:
        # Numeric columns are returned as Decimal by SQLAlchemy; keep all math exact.
        if not isinstance(amount, Decimal):
            raise TypeError("Transaction amounts must be loaded as Decimal values.")
        successful_count += 1
        successful_value += amount
        if transaction_type == "deposit":
            deposit_total += amount
        elif transaction_type == "withdrawal":
            withdrawal_total += amount

    average = (
        (successful_value / successful_count).quantize(CENT, rounding=ROUND_HALF_UP)
        if successful_count
        else None
    )

    daily_rows = session.execute(
        select(func.date(Transaction.occurred_at), func.count(Transaction.id))
        .where(*filters)
        .group_by(func.date(Transaction.occurred_at))
        .order_by(func.date(Transaction.occurred_at))
    )
    daily_counts = {}
    for day_value, count in daily_rows:
        if isinstance(day_value, datetime):
            day = day_value.date()
        elif isinstance(day_value, date):
            day = day_value
        else:
            day = date.fromisoformat(str(day_value))
        daily_counts[day] = count

    if start_utc is not None and end_utc is not None:
        current_day = start_utc.date()
        last_day = (end_utc - timedelta(microseconds=1)).date()
        while current_day <= last_day:
            daily_counts.setdefault(current_day, 0)
            current_day += timedelta(days=1)

    return AnalyticsSummary(
        currency="USD",
        total_transaction_count=total_count,
        successful_transaction_count=successful_count,
        successful_transaction_value=successful_value,
        average_successful_transaction_amount=average,
        counts_by_status=counts_by_status,
        counts_by_type=counts_by_type,
        counts_by_channel=counts_by_channel,
        successful_deposit_total=deposit_total,
        successful_withdrawal_total=withdrawal_total,
        daily_volume=[
            DailyVolume(day=day, transaction_count=count)
            for day, count in sorted(daily_counts.items())
        ],
    )
