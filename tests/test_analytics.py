from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from app.analytics import summarize_transactions
from app.database import create_schema, make_engine
from app.models import Account, Transaction


def make_test_engine():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    create_schema(engine)
    return engine


def add_transactions(engine, entries):
    with Session(engine) as session, session.begin():
        source = Account(synthetic_reference="SYN-ANALYTICS-0001")
        destination = Account(synthetic_reference="SYN-ANALYTICS-0002")
        session.add_all([source, destination])
        session.flush()

        for index, entry in enumerate(entries, start=1):
            transaction_type, status, amount, occurred_at = entry[:4]
            currency = entry[4] if len(entry) > 4 else "USD"
            source_account = None if transaction_type == "deposit" else source
            destination_account = None if transaction_type == "withdrawal" else destination
            session.add(
                Transaction(
                    reference=f"SYN-ANALYTICS-TX-{index:04d}",
                    source_account=source_account,
                    destination_account=destination_account,
                    transaction_type=transaction_type,
                    amount=Decimal(amount),
                    currency=currency,
                    occurred_at=occurred_at,
                    status=status,
                )
            )


def utc(day, hour=0, minute=0, second=0, microsecond=0):
    return datetime(2025, 1, day, hour, minute, second, microsecond, tzinfo=timezone.utc)


def test_summary_counts_all_events_but_sums_only_successful_transactions():
    engine = make_test_engine()
    add_transactions(
        engine,
        [
            ("deposit", "successful", "10.10", utc(1)),
            ("withdrawal", "successful", "2.25", utc(1, 12)),
            ("transfer", "successful", "3.33", utc(2)),
            ("payment", "successful", "4.00", utc(2, 12)),
            ("deposit", "pending", "100.00", utc(1, 23, 59, 59)),
            ("withdrawal", "failed", "50.00", utc(2, 23, 59, 59)),
        ],
    )

    with Session(engine) as session:
        summary = summarize_transactions(
            session,
            start_at=utc(1),
            end_at=utc(3),
        )

    assert summary.currency == "USD"
    assert summary.total_transaction_count == 6
    assert summary.successful_transaction_count == 4
    assert summary.successful_transaction_value == Decimal("19.68")
    assert summary.average_successful_transaction_amount == Decimal("4.92")
    assert isinstance(summary.successful_transaction_value, Decimal)
    assert summary.counts_by_status == {"failed": 1, "pending": 1, "successful": 4}
    assert summary.counts_by_type == {"deposit": 2, "payment": 1, "transfer": 1, "withdrawal": 2}
    assert summary.counts_by_channel == {"UPI": 0, "ATM": 0, "BRANCH": 0, "UNKNOWN": 6}
    assert summary.successful_deposit_total == Decimal("10.10")
    assert summary.successful_withdrawal_total == Decimal("2.25")
    assert [(row.day, row.transaction_count) for row in summary.daily_volume] == [
        (utc(1).date(), 3),
        (utc(2).date(), 3),
    ]


def test_empty_database_returns_zero_metrics_and_empty_groups():
    engine = make_test_engine()
    with Session(engine) as session:
        summary = summarize_transactions(session)

    assert summary.total_transaction_count == 0
    assert summary.successful_transaction_count == 0
    assert summary.successful_transaction_value == Decimal("0.00")
    assert summary.average_successful_transaction_amount is None
    assert summary.counts_by_status == {}
    assert summary.counts_by_type == {}
    assert summary.successful_deposit_total == Decimal("0.00")
    assert summary.successful_withdrawal_total == Decimal("0.00")
    assert summary.daily_volume == []
    assert summary.counts_by_channel == {"UPI": 0, "ATM": 0, "BRANCH": 0, "UNKNOWN": 0}


def test_summary_excludes_events_at_and_after_utc_demo_cutoff():
    engine = make_test_engine()
    cutoff = datetime(2026, 10, 19, tzinfo=timezone.utc)
    add_transactions(
        engine,
        [
            ("deposit", "successful", "1.25", cutoff - timedelta(seconds=1)),
            ("withdrawal", "successful", "2.75", datetime(2026, 10, 18, 23, 59, 59, 999999, tzinfo=timezone.utc)),
            ("transfer", "successful", "100.00", cutoff),
            ("payment", "successful", "200.00", cutoff + timedelta(days=1)),
        ],
    )

    with Session(engine) as session:
        summary = summarize_transactions(session)

    assert summary.total_transaction_count == 2
    assert summary.successful_transaction_count == 2
    assert summary.successful_transaction_value == Decimal("4.00")
    assert summary.average_successful_transaction_amount == Decimal("2.00")
    assert summary.successful_deposit_total == Decimal("1.25")
    assert summary.successful_withdrawal_total == Decimal("2.75")
    assert summary.counts_by_type == {"deposit": 1, "withdrawal": 1}
    assert [(row.day, row.transaction_count) for row in summary.daily_volume] == [
        (date(2026, 10, 18), 2),
    ]


def test_start_is_inclusive_end_is_exclusive_and_timezone_is_normalized():
    engine = make_test_engine()
    add_transactions(
        engine,
        [
            ("deposit", "successful", "1.00", utc(1) - timedelta(microseconds=1)),
            ("deposit", "successful", "2.00", utc(1)),
            ("transfer", "successful", "3.00", utc(2)),
            ("payment", "successful", "4.00", utc(2) + timedelta(microseconds=1)),
        ],
    )

    # This non-UTC offset is exactly midnight UTC on January 1.
    start = datetime(2025, 1, 1, 5, 30, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    with Session(engine) as session:
        summary = summarize_transactions(session, start_at=start, end_at=utc(2))

    assert summary.total_transaction_count == 1
    assert summary.successful_transaction_value == Decimal("2.00")
    assert [(row.day, row.transaction_count) for row in summary.daily_volume] == [
        (utc(1).date(), 1)
    ]


def test_empty_date_range_returns_zero_filled_utc_days():
    engine = make_test_engine()
    with Session(engine) as session:
        summary = summarize_transactions(session, start_at=utc(4), end_at=utc(7))

    assert summary.total_transaction_count == 0
    assert summary.average_successful_transaction_amount is None
    assert [(row.day, row.transaction_count) for row in summary.daily_volume] == [
        (utc(4).date(), 0),
        (utc(5).date(), 0),
        (utc(6).date(), 0),
    ]


def test_average_uses_decimal_half_up_rounding_and_rejects_non_usd():
    engine = make_test_engine()
    add_transactions(
        engine,
        [
            ("deposit", "successful", "1.00", utc(1)),
            ("withdrawal", "successful", "1.01", utc(1, 1)),
        ],
    )
    with Session(engine) as session:
        summary = summarize_transactions(session)
    assert summary.successful_transaction_value == Decimal("2.01")
    assert summary.average_successful_transaction_amount == Decimal("1.01")

    foreign_currency_engine = make_test_engine()
    add_transactions(foreign_currency_engine, [("deposit", "successful", "1.00", utc(1), "EUR")])
    with Session(foreign_currency_engine) as session:
        with pytest.raises(ValueError, match="supports USD only"):
            summarize_transactions(session)


@pytest.mark.parametrize(
    ("start_at", "end_at", "message"),
    [
        (utc(2), utc(1), "earlier"),
        (datetime(2025, 1, 1), utc(2), "timezone"),
        (utc(1), datetime(2025, 1, 2), "timezone"),
    ],
)
def test_rejects_invalid_date_ranges(start_at, end_at, message):
    engine = make_test_engine()
    with Session(engine) as session:
        with pytest.raises(ValueError, match=message):
            summarize_transactions(session, start_at=start_at, end_at=end_at)
