"""Generate deterministic synthetic accounts and transaction events."""

import os
import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from .database import create_schema, make_engine
from .models import Account, Transaction


LEGACY_TRANSACTION_COUNT = 1_200
HISTORICAL_TRANSACTION_COUNT = 1_200
DEFAULT_TRANSACTION_COUNT = LEGACY_TRANSACTION_COUNT + HISTORICAL_TRANSACTION_COUNT
LEGACY_JANUARY_COUNT = 100
DAILY_EXTENSION_COUNT = 700
EXTENSION_START_OFFSET = 31
DATASET_START = datetime(2025, 1, 1, tzinfo=timezone.utc)
DATASET_END = datetime(2026, 10, 18, 23, 59, 59, tzinfo=timezone.utc)
HISTORICAL_START = datetime(2023, 1, 1, tzinfo=timezone.utc)
HISTORICAL_DAY_COUNT = (DATASET_START.date() - HISTORICAL_START.date()).days
CHANNEL_PATTERN = ("UPI",) * 12 + ("ATM",) * 5 + ("BRANCH",) * 3


def _channel_for_sequence(number: int) -> str:
    """Return a repeatable 60/25/15 channel assignment without using an RNG."""
    if number < 1:
        raise ValueError("Transaction sequence numbers must be positive.")
    return CHANNEL_PATTERN[(number - 1) % len(CHANNEL_PATTERN)]


@dataclass(frozen=True)
class SeedSummary:
    """Counts inserted by one successful seed operation."""

    accounts: int
    transactions: int


def seed_database(
    engine,
    *,
    account_count: int = 20,
    transaction_count: int = DEFAULT_TRANSACTION_COUNT,
    seed: int = 2026,
) -> SeedSummary:
    """Add deterministic synthetic rows without changing existing database records.

    The original 1,200 generated records retain their deterministic references and
    values. New databases receive only those original records dated through October
    18, 2026; later-dated references are omitted from new inserts. An additional
    1,200 records extend the sample across 2023 and 2024. Existing account and
    transaction references are left untouched and skipped on reruns.
    """
    if account_count < 2:
        raise ValueError("At least two accounts are required for transfer examples.")
    if transaction_count < 1:
        raise ValueError("transaction_count must be positive.")

    create_schema(engine)
    generator = random.Random(seed)
    transaction_types = ("deposit", "withdrawal", "transfer", "payment")
    type_weights = (20, 20, 35, 25)
    statuses = ("successful", "pending", "failed")
    status_weights = (88, 7, 5)

    with Session(engine) as session, session.begin():
        account_references = [f"SYN-ACCT-{number:04d}" for number in range(1, account_count + 1)]
        existing_accounts = {
            account.synthetic_reference: account
            for account in session.scalars(
                select(Account).where(Account.synthetic_reference.in_(account_references))
            )
        }
        accounts_added = 0
        for reference in account_references:
            if reference not in existing_accounts:
                account = Account(synthetic_reference=reference)
                session.add(account)
                existing_accounts[reference] = account
                accounts_added += 1
        session.flush()
        accounts = [existing_accounts[reference] for reference in account_references]

        descriptions = {
            "deposit": "Synthetic cash deposit",
            "withdrawal": "Synthetic cash withdrawal",
            "transfer": "Synthetic account transfer",
            "payment": "Synthetic merchant payment",
        }
        record_specs = []
        legacy_count = min(transaction_count, LEGACY_TRANSACTION_COUNT)
        for number in range(1, legacy_count + 1):
            transaction_type = generator.choices(transaction_types, weights=type_weights, k=1)[0]
            status = generator.choices(statuses, weights=status_weights, k=1)[0]
            source = destination = None
            if transaction_type == "deposit":
                destination = generator.choice(accounts)
            elif transaction_type == "withdrawal":
                source = generator.choice(accounts)
            else:
                source, destination = generator.sample(accounts, 2)

            cents = generator.randint(250, 250_000)
            if number <= LEGACY_JANUARY_COUNT:
                occurred_at = DATASET_START + timedelta(
                    days=generator.randrange(31),
                    seconds=generator.randrange(86_400),
                )
            elif number <= LEGACY_JANUARY_COUNT + DAILY_EXTENSION_COUNT:
                # Preserve the continuous one-event-per-day backbone.
                occurred_at = DATASET_START + timedelta(
                    days=EXTENSION_START_OFFSET + number - LEGACY_JANUARY_COUNT - 1,
                    seconds=generator.randrange(86_400),
                )
            else:
                # Add irregular daily volume while keeping every day in the range populated.
                occurred_at = DATASET_START + timedelta(
                    days=EXTENSION_START_OFFSET + generator.randrange(DAILY_EXTENSION_COUNT),
                    seconds=generator.randrange(86_400),
                )
            if occurred_at <= DATASET_END:
                record_specs.append(
                    {
                        "reference": f"SYN-TX-{occurred_at.year}-{number:06d}",
                        "source_account": source,
                        "destination_account": destination,
                        "transaction_type": transaction_type,
                        "amount": Decimal(cents) / Decimal("100"),
                        "occurred_at": occurred_at,
                        "status": status,
                        "description": descriptions[transaction_type],
                        "channel": _channel_for_sequence(number),
                    }
                )

        historical_count = max(0, transaction_count - LEGACY_TRANSACTION_COUNT)
        historical_generator = random.Random(f"{seed}:historical")
        for offset in range(historical_count):
            number = LEGACY_TRANSACTION_COUNT + offset + 1
            transaction_type = historical_generator.choices(
                transaction_types, weights=type_weights, k=1
            )[0]
            status = historical_generator.choices(statuses, weights=status_weights, k=1)[0]
            source = destination = None
            if transaction_type == "deposit":
                destination = historical_generator.choice(accounts)
            elif transaction_type == "withdrawal":
                source = historical_generator.choice(accounts)
            else:
                source, destination = historical_generator.sample(accounts, 2)

            cents = historical_generator.randint(250, 250_000)
            day_offset = (
                offset
                if offset < HISTORICAL_DAY_COUNT
                else historical_generator.randrange(HISTORICAL_DAY_COUNT)
            )
            occurred_at = HISTORICAL_START + timedelta(
                days=day_offset,
                seconds=historical_generator.randrange(86_400),
            )
            record_specs.append(
                {
                    "reference": f"SYN-TX-{occurred_at.year}-{number:06d}",
                    "source_account": source,
                    "destination_account": destination,
                    "transaction_type": transaction_type,
                    "amount": Decimal(cents) / Decimal("100"),
                    "occurred_at": occurred_at,
                    "status": status,
                    "description": descriptions[transaction_type],
                    "channel": _channel_for_sequence(number),
                }
            )
        with session.no_autoflush:
            existing_references = set(
                session.scalars(
                    select(Transaction.reference).where(
                        Transaction.reference.in_([spec["reference"] for spec in record_specs])
                    )
                )
            )
        records_to_add = [
            Transaction(**spec)
            for spec in record_specs
            if spec["reference"] not in existing_references
        ]
        session.add_all(records_to_add)

    return SeedSummary(accounts=accounts_added, transactions=len(records_to_add))


def main() -> None:
    """Create the configured schema and seed it once from the command line."""
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise SystemExit("Set DATABASE_URL to a MySQL connection URL before seeding.")

    engine = make_engine(database_url)
    summary = seed_database(engine)
    print(
        f"Inserted {summary.accounts} synthetic accounts and "
        f"{summary.transactions} synthetic transactions (USD, January 1, 2023 "
        f"through October 18, 2026; default requested total {DEFAULT_TRANSACTION_COUNT})."
    )


if __name__ == "__main__":
    main()
