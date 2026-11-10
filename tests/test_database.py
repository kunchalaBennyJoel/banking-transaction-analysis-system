from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import create_schema, make_engine
from app.models import Account, Transaction
from app.seed import seed_database


def make_test_engine():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    create_schema(engine)
    return engine


def test_seed_is_deterministic_idempotent_and_preserves_existing_records():
    engine = make_test_engine()

    result = seed_database(engine, account_count=6, transaction_count=40, seed=123)

    assert result.accounts == 6
    assert result.transactions == 40
    with Session(engine) as session:
        rows = session.scalars(select(Transaction).order_by(Transaction.reference)).all()
        assert len(rows) == 40
        assert {row.currency for row in rows} == {"USD"}
        assert {row.status for row in rows} == {"successful", "pending", "failed"}
        assert {row.transaction_type for row in rows} == {
            "deposit", "withdrawal", "transfer", "payment"
        }
        assert {row.channel for row in rows} <= {"UPI", "ATM", "BRANCH"}
        assert {channel: sum(row.channel == channel for row in rows) for channel in ("UPI", "ATM", "BRANCH")} == {
            "UPI": 24, "ATM": 10, "BRANCH": 6,
        }
        assert all(row.amount > Decimal("0") for row in rows)
        first_run = [
            (row.reference, row.transaction_type, row.amount, row.occurred_at, row.status, row.channel)
            for row in rows
        ]

    second_engine = make_test_engine()
    seed_database(second_engine, account_count=6, transaction_count=40, seed=123)
    with Session(second_engine) as session:
        second_run = [
            (row.reference, row.transaction_type, row.amount, row.occurred_at, row.status, row.channel)
            for row in session.scalars(select(Transaction).order_by(Transaction.reference)).all()
        ]
    assert first_run == second_run

    with Session(engine) as session:
        first_row = session.scalars(select(Transaction).order_by(Transaction.reference)).first()
        original_value = first_row.amount
        preserved_channel = "BRANCH" if first_row.channel != "BRANCH" else "UPI"
        first_row.amount = Decimal("999.99")
        first_row.channel = preserved_channel
        session.commit()

    rerun = seed_database(engine, account_count=6, transaction_count=40, seed=123)
    assert rerun.accounts == 0
    assert rerun.transactions == 0
    with Session(engine) as session:
        assert session.scalar(select(Transaction.amount).where(Transaction.reference == first_run[0][0])) == Decimal("999.99")
        assert session.scalar(select(Transaction.channel).where(Transaction.reference == first_run[0][0])) == preserved_channel
        assert session.scalar(select(Transaction.id).where(Transaction.reference == "SYN-TX-2025-000040")) is not None
        assert original_value != Decimal("999.99")


def test_default_seed_extends_legacy_rows_continuously_and_is_idempotent():
    engine = make_test_engine()
    seed_database(engine, transaction_count=1_200)

    with Session(engine) as session:
        legacy_rows = {
            row.reference: (row.amount, row.occurred_at, row.status, row.transaction_type, row.channel)
            for row in session.scalars(select(Transaction)).all()
        }

    result = seed_database(engine)
    assert result.accounts == 0
    assert result.transactions == 1_200

    with Session(engine) as session:
        rows = session.scalars(
            select(Transaction).order_by(Transaction.occurred_at, Transaction.reference)
        ).all()
        assert 2_200 <= len(rows) <= 2_400
        assert min(row.occurred_at.date() for row in rows) == date(2023, 1, 1)
        assert max(row.occurred_at.date() for row in rows) == date(2026, 10, 18)
        assert all(row.occurred_at.date() <= date(2026, 10, 18) for row in rows)
        assert all(row.channel in {"UPI", "ATM", "BRANCH"} for row in rows)
        assert {row.occurred_at.year for row in rows} == {2023, 2024, 2025, 2026}
        assert len({row.reference for row in rows}) == len(rows)
        for row in rows:
            if row.reference in legacy_rows:
                assert (row.amount, row.occurred_at, row.status, row.transaction_type, row.channel) == legacy_rows[row.reference]
        first_complete_run = [
            (
                row.reference,
                row.source_account_id,
                row.destination_account_id,
                row.transaction_type,
                row.amount,
                row.occurred_at,
                row.status,
                row.description,
                row.channel,
            )
            for row in rows
        ]

    assert seed_database(engine).transactions == 0
    with Session(engine) as session:
        second_complete_run = [
            (
                row.reference,
                row.source_account_id,
                row.destination_account_id,
                row.transaction_type,
                row.amount,
                row.occurred_at,
                row.status,
                row.description,
                row.channel,
            )
            for row in session.scalars(
                select(Transaction).order_by(Transaction.occurred_at, Transaction.reference)
            ).all()
        ]
    assert second_complete_run == first_complete_run


def test_channel_backfill_script_is_guarded_idempotent_and_updates_only_channel():
    script_path = Path(__file__).resolve().parents[1] / "migrations" / "003_backfill_transaction_channels.sql"
    script = script_path.read_text(encoding="utf-8").lower()

    assert "row_number() over (order by id)" in script
    assert "v_transaction_count <> 2400 or v_null_channel_count <> 2400" in script
    assert "v_transaction_count = 2400" in script
    assert "v_null_channel_count = 0" in script
    assert "v_upi_count = 1440" in script
    assert "v_atm_count = 600" in script
    assert "v_branch_count = 360" in script
    assert "set transaction isolation level serializable" in script
    assert "cast(channel as binary) = cast('upi' as binary)" in script
    assert "when ranked.sequence_number <= 1440 then 'upi'" in script
    assert "when ranked.sequence_number <= 2040 then 'atm'" in script
    assert "else 'branch'" in script
    assert "transaction_id int not null primary key" in script
    assert "set target.channel = assignment.assigned_channel" in script
    assert "where target.channel is null" in script
    assert "set v_updated_count = row_count()" in script
    assert "if v_updated_count <> 2400" in script
    assert "declare exit handler for sqlexception" in script
    assert "rollback;" in script
    assert "signal sqlstate '45000'" in script
    assert "drop procedure codex_backfill_transaction_channels_003" in script
    assert "update accounts" not in script
    assert "delete from transactions" not in script
    assert "truncate" not in script


def test_seed_preserves_existing_legacy_rows_after_the_new_cutoff():
    engine = make_test_engine()
    with Session(engine) as session, session.begin():
        account = Account(synthetic_reference="SYN-ACCT-0001")
        session.add(account)
        session.flush()
        future_legacy_row = Transaction(
            reference="SYN-TX-2027-000800",
            destination_account_id=account.id,
            transaction_type="deposit",
            amount=Decimal("42.50"),
            currency="USD",
            occurred_at=datetime(2027, 1, 1, 12),
            status="successful",
            description="Existing synthetic legacy row",
        )
        session.add(future_legacy_row)

    seed_database(engine)

    with Session(engine) as session:
        row = session.scalar(
            select(Transaction).where(Transaction.reference == "SYN-TX-2027-000800")
        )
        assert row is not None
        assert row.amount == Decimal("42.50")
        assert row.occurred_at == datetime(2027, 1, 1, 12)
        assert row.description == "Existing synthetic legacy row"
        after_cutoff = session.scalars(
            select(Transaction).where(Transaction.occurred_at >= datetime(2026, 10, 19))
        ).all()
        assert [future_row.reference for future_row in after_cutoff] == [
            "SYN-TX-2027-000800"
        ]


def test_schema_rejects_invalid_amount_and_missing_account_reference():
    engine = make_test_engine()
    with Session(engine) as session, session.begin():
        session.add(Account(synthetic_reference="SYN-ACCT-0001"))

    invalid_rows = (
        Transaction(
            reference="SYN-TX-INVALID-AMOUNT",
            source_account_id=1,
            transaction_type="withdrawal",
            amount=Decimal("0.00"),
            currency="USD",
            occurred_at=datetime(2025, 1, 1),
            status="successful",
        ),
        Transaction(
            reference="SYN-TX-MISSING-ACCOUNT",
            source_account_id=999,
            transaction_type="withdrawal",
            amount=Decimal("1.00"),
            currency="USD",
            occurred_at=datetime(2025, 1, 1),
            status="successful",
        ),
    )
    for transaction in invalid_rows:
        with pytest.raises(IntegrityError):
            with Session(engine) as session:
                session.add(transaction)
                session.commit()
