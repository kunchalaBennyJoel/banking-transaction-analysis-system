import csv
import importlib
import io
import zipfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import secrets

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from werkzeug.security import generate_password_hash

from app import create_app
from app.database import create_schema
from app.models import Account, Employee, Transaction
from app.seed import seed_database


@pytest.fixture
def app():
    application = create_app(
        {
            "TESTING": True,
            "DATABASE_URL": "sqlite+pysqlite:///:memory:",
            "SECRET_KEY": secrets.token_hex(32),
        }
    )
    create_schema(application.extensions["database_engine"])
    with Session(application.extensions["database_engine"]) as session, session.begin():
        employee = Employee(
            employee_id="TEST-ADMIN",
            password_hash=generate_password_hash("test-password-never-used"),
            role="admin",
        )
        session.add(employee)
        session.flush()
        application.extensions["test_employee_pk"] = employee.id
    yield application
    application.extensions["database_engine"].dispose()


@pytest.fixture
def client(app):
    test_client = app.test_client()
    with test_client.session_transaction() as flask_session:
        flask_session["employee_pk"] = app.extensions["test_employee_pk"]
        flask_session.permanent = True
    return test_client


def utc(day, hour=0):
    return datetime(2025, 1, day, hour, tzinfo=timezone.utc)


def insert_transactions(app, entries):
    engine = app.extensions["database_engine"]
    with Session(engine) as session, session.begin():
        source = Account(synthetic_reference="SYN-API-ACCOUNT-0001")
        destination = Account(synthetic_reference="SYN-API-ACCOUNT-0002")
        session.add_all([source, destination])
        session.flush()

        for index, entry in enumerate(entries, start=1):
            transaction_type, status, amount, occurred_at = entry[:4]
            channel = entry[4] if len(entry) > 4 else None
            source_account = None if transaction_type == "deposit" else source
            destination_account = None if transaction_type == "withdrawal" else destination
            session.add(
                Transaction(
                    reference=f"SYN-API-TX-{index:04d}",
                    source_account=source_account,
                    destination_account=destination_account,
                    transaction_type=transaction_type,
                    amount=Decimal(str(amount)),
                    currency="USD",
                    occurred_at=occurred_at,
                    status=status,
                    description=f"Synthetic test event {index}",
                    channel=channel,
                )
            )


def populated_entries():
    return [
        ("deposit", "successful", "10.10", utc(1)),
        ("withdrawal", "successful", "2.25", utc(1, 12)),
        ("transfer", "successful", "3.33", utc(2)),
        ("payment", "successful", "4.00", utc(2, 12)),
        ("deposit", "pending", "100.00", utc(1, 23)),
        ("withdrawal", "failed", "50.00", utc(2, 23)),
    ]


def assert_error(response, status, code):
    assert response.status_code == status
    assert response.is_json
    body = response.get_json()
    assert set(body) == {"error"}
    assert body["error"]["code"] == code
    assert isinstance(body["error"]["message"], str)


def test_health_remains_available_without_database_configuration():
    app_without_database = create_app({"TESTING": True, "DATABASE_URL": None})

    response = app_without_database.test_client().get("/health")

    assert response.status_code == 200
    assert response.get_json() == {"status": "ok"}


def test_summary_returns_real_metrics_and_decimal_strings(client, app):
    insert_transactions(app, populated_entries())

    response = client.get("/api/analytics/summary")

    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data == {
        "currency": "USD",
        "total_transaction_count": 6,
        "successful_transaction_count": 4,
        "successful_transaction_value": "19.68",
        "average_successful_transaction_amount": "4.92",
        "counts_by_status": {"failed": 1, "pending": 1, "successful": 4},
        "counts_by_type": {"deposit": 2, "payment": 1, "transfer": 1, "withdrawal": 2},
        "counts_by_channel": {"UPI": 0, "ATM": 0, "BRANCH": 0, "UNKNOWN": 6},
        "successful_deposit_total": "10.10",
        "successful_withdrawal_total": "2.25",
        "daily_volume": [
            {"date": "2025-01-01", "transaction_count": 3},
            {"date": "2025-01-02", "transaction_count": 3},
        ],
    }
    assert isinstance(data["successful_transaction_value"], str)
    assert not isinstance(data["successful_transaction_value"], float)


def test_cutoff_applies_to_summary_volume_listing_and_pagination(client, app):
    cutoff = datetime(2026, 10, 19, tzinfo=timezone.utc)
    insert_transactions(
        app,
        [
            ("deposit", "successful", "1.25", cutoff - timedelta(seconds=1)),
            ("withdrawal", "successful", "2.75", datetime(2026, 10, 18, 23, 59, 59, 999999, tzinfo=timezone.utc)),
            ("transfer", "successful", "100.00", cutoff),
            ("payment", "successful", "200.00", cutoff + timedelta(days=1)),
        ],
    )

    summary = client.get("/api/analytics/summary").get_json()["data"]
    volume = client.get(
        "/api/analytics/volume?start_date=2026-10-18&end_date=2026-10-20"
    ).get_json()["data"]
    first_page = client.get("/api/transactions?page_size=1").get_json()
    second_page = client.get("/api/transactions?page=2&page_size=1").get_json()
    bounded_listing = client.get(
        "/api/transactions?start_date=2026-10-18&end_date=2026-10-20&page_size=1"
    ).get_json()

    assert summary["total_transaction_count"] == 2
    assert summary["successful_transaction_value"] == "4.00"
    assert summary["average_successful_transaction_amount"] == "2.00"
    assert volume["total_transaction_count"] == 2
    assert volume["successful_transaction_value"] == "4.00"
    assert volume["daily_volume"][0] == {"date": "2026-10-18", "transaction_count": 2}
    assert first_page["pagination"]["total_records"] == 2
    assert first_page["pagination"]["total_pages"] == 2
    assert second_page["pagination"]["total_records"] == 2
    assert [first_page["data"][0]["reference"], second_page["data"][0]["reference"]] == [
        "SYN-API-TX-0002",
        "SYN-API-TX-0001",
    ]
    assert first_page["data"][0]["occurred_at"] == "2026-10-18T23:59:59.999999Z"
    assert second_page["data"][0]["occurred_at"] == "2026-10-18T23:59:59.000000Z"
    assert bounded_listing["pagination"]["total_records"] == 2
    assert bounded_listing["pagination"]["total_pages"] == 2


def test_future_only_date_filters_return_zero_without_exposing_records(client, app):
    cutoff = datetime(2026, 10, 19, tzinfo=timezone.utc)
    insert_transactions(
        app,
        [("deposit", "successful", "50.00", cutoff + datetime.resolution)],
    )

    volume = client.get(
        "/api/analytics/volume?start_date=2026-10-19&end_date=2026-10-21"
    ).get_json()["data"]
    listing = client.get(
        "/api/transactions?start_date=2026-10-19&end_date=2026-10-21"
    ).get_json()

    assert volume["total_transaction_count"] == 0
    assert volume["successful_transaction_value"] == "0.00"
    assert volume["daily_volume"] == [
        {"date": "2026-10-19", "transaction_count": 0},
        {"date": "2026-10-20", "transaction_count": 0},
    ]
    assert listing["data"] == []
    assert listing["pagination"]["total_records"] == 0
    assert listing["pagination"]["total_pages"] == 0


def test_empty_summary_has_zero_values_and_null_average(client):
    response = client.get("/api/analytics/summary")

    assert response.status_code == 200
    assert response.get_json()["data"] == {
        "currency": "USD",
        "total_transaction_count": 0,
        "successful_transaction_count": 0,
        "successful_transaction_value": "0.00",
        "average_successful_transaction_amount": None,
        "counts_by_status": {},
        "counts_by_type": {},
        "counts_by_channel": {"UPI": 0, "ATM": 0, "BRANCH": 0, "UNKNOWN": 0},
        "successful_deposit_total": "0.00",
        "successful_withdrawal_total": "0.00",
        "daily_volume": [],
    }


def test_summary_fills_zero_count_days_between_all_time_events(client, app):
    insert_transactions(
        app,
        [
            ("deposit", "successful", "1.00", utc(1)),
            ("payment", "pending", "2.00", utc(3)),
        ],
    )

    response = client.get("/api/analytics/summary")

    assert response.status_code == 200
    assert response.get_json()["data"]["daily_volume"] == [
        {"date": "2025-01-01", "transaction_count": 1},
        {"date": "2025-01-02", "transaction_count": 0},
        {"date": "2025-01-03", "transaction_count": 1},
    ]


def test_volume_uses_utc_inclusive_start_and_exclusive_end(client, app):
    insert_transactions(
        app,
        [
            ("deposit", "successful", "1.00", utc(1)),
            ("withdrawal", "pending", "2.00", utc(2, 12)),
            ("transfer", "successful", "3.00", utc(3)),
        ],
    )

    response = client.get(
        "/api/analytics/volume?start_date=2025-01-01&end_date=2025-01-03"
    )

    assert response.status_code == 200
    assert response.get_json()["data"] == {
        "start_date_inclusive": "2025-01-01",
        "end_date_exclusive": "2025-01-03",
        "timezone": "UTC",
        "currency": "USD",
        "total_transaction_count": 2,
        "successful_transaction_count": 1,
        "successful_transaction_value": "1.00",
        "average_successful_transaction_amount": "1.00",
        "counts_by_status": {"pending": 1, "successful": 1},
        "counts_by_type": {"deposit": 1, "withdrawal": 1},
        "counts_by_channel": {"UPI": 0, "ATM": 0, "BRANCH": 0, "UNKNOWN": 2},
        "successful_deposit_total": "1.00",
        "successful_withdrawal_total": "0.00",
        "daily_volume": [
            {"date": "2025-01-01", "transaction_count": 1},
            {"date": "2025-01-02", "transaction_count": 1},
        ],
    }


def test_seeded_data_appears_across_years_and_empty_ranges_stay_empty(client, app):
    seed_database(app.extensions["database_engine"])

    january_2023 = client.get(
        "/api/analytics/volume?start_date=2023-01-01&end_date=2023-02-01"
    ).get_json()["data"]
    january_2025 = client.get(
        "/api/analytics/volume?start_date=2025-01-01&end_date=2025-02-01"
    ).get_json()["data"]
    january_2026 = client.get(
        "/api/analytics/volume?start_date=2026-01-01&end_date=2026-02-01"
    ).get_json()["data"]
    january_2026_transactions = client.get(
        "/api/transactions?start_date=2026-01-01&end_date=2026-02-01"
    ).get_json()
    october_2026 = client.get(
        "/api/analytics/volume?start_date=2026-10-01&end_date=2026-10-19"
    ).get_json()["data"]

    assert january_2023["total_transaction_count"] >= 31
    assert len(january_2023["daily_volume"]) == 31
    assert all(day["transaction_count"] >= 1 for day in january_2023["daily_volume"])
    assert january_2025["total_transaction_count"] == 100
    assert january_2026["total_transaction_count"] >= 31
    assert len(january_2026["daily_volume"]) == 31
    assert all(day["transaction_count"] >= 1 for day in january_2026["daily_volume"])
    assert sum(day["transaction_count"] for day in january_2026["daily_volume"]) == january_2026[
        "total_transaction_count"
    ]
    assert january_2026_transactions["pagination"]["total_records"] == january_2026[
        "total_transaction_count"
    ]
    assert october_2026["total_transaction_count"] >= 18
    assert len(october_2026["daily_volume"]) == 18
    assert all(day["transaction_count"] >= 1 for day in october_2026["daily_volume"])

    empty_analysis = client.get(
        "/api/analytics/volume?start_date=2027-02-01&end_date=2027-03-01"
    ).get_json()["data"]
    empty_transactions = client.get(
        "/api/transactions?start_date=2027-02-01&end_date=2027-03-01"
    ).get_json()

    assert empty_analysis["total_transaction_count"] == 0
    assert empty_analysis["successful_transaction_count"] == 0
    assert empty_analysis["successful_transaction_value"] == "0.00"
    assert empty_analysis["counts_by_status"] == {}
    assert empty_analysis["counts_by_type"] == {}
    assert len(empty_analysis["daily_volume"]) == 28
    assert all(day["transaction_count"] == 0 for day in empty_analysis["daily_volume"])
    assert empty_transactions["data"] == []
    assert empty_transactions["pagination"]["total_records"] == 0


@pytest.mark.parametrize(
    "query",
    [
        "",
        "?start_date=2025-01-01",
        "?end_date=2025-01-02",
        "?start_date=01-01-2025&end_date=2025-01-02",
        "?start_date=2025-02-30&end_date=2025-03-01",
        "?start_date=2025-01-03&end_date=2025-01-02",
        "?start_date=2025-01-02&end_date=2025-01-02",
    ],
)
def test_volume_rejects_missing_malformed_or_reversed_dates(client, query):
    assert_error(client.get(f"/api/analytics/volume{query}"), 400, "invalid_query_parameter")


def test_empty_volume_range_returns_zero_days(client):
    response = client.get(
        "/api/analytics/volume?start_date=2025-01-01&end_date=2025-01-04"
    )

    assert response.status_code == 200
    assert response.get_json()["data"]["daily_volume"] == [
        {"date": "2025-01-01", "transaction_count": 0},
        {"date": "2025-01-02", "transaction_count": 0},
        {"date": "2025-01-03", "transaction_count": 0},
    ]


def test_transaction_listing_uses_default_pagination_and_stable_tie_breaker(client, app):
    same_time = utc(1)
    insert_transactions(
        app,
        [("deposit", "successful", "2.50", same_time) for _ in range(30)],
    )

    first_page = client.get("/api/transactions")
    second_page = client.get("/api/transactions?page=2")

    assert first_page.status_code == 200
    first = first_page.get_json()
    second = second_page.get_json()
    assert first["pagination"] == {
        "page": 1,
        "page_size": 25,
        "total_records": 30,
        "total_pages": 2,
        "has_previous": False,
        "has_next": True,
    }
    assert len(first["data"]) == 25
    assert len(second["data"]) == 5
    assert first["data"][0]["reference"] == "SYN-API-TX-0030"
    assert first["data"][-1]["reference"] == "SYN-API-TX-0006"
    assert second["data"][0]["reference"] == "SYN-API-TX-0005"
    assert first["data"][0]["amount"] == "2.50"
    assert first["data"][0]["occurred_at"] == "2025-01-01T00:00:00.000000Z"
    assert set(first["data"][0]) == {
        "id", "reference", "source_account_id", "destination_account_id",
        "transaction_type", "amount", "currency", "occurred_at", "status", "description",
        "channel",
    }


@pytest.mark.parametrize(
    ("query", "message"),
    [
        ("page=0", "page"),
        ("page=-1", "page"),
        ("page=abc", "page"),
        ("page_size=0", "page_size"),
        ("page_size=101", "not exceed 100"),
        ("status=complete", "status"),
        ("transaction_type=refund", "transaction_type"),
        ("channel=POS", "channel"),
        ("start_date=bad&end_date=2025-01-02", "YYYY-MM-DD"),
        ("start_date=2025-02-30&end_date=2025-03-01", "calendar date"),
        ("start_date=2025-01-03&end_date=2025-01-02", "earlier"),
        ("start_date=2025-01-01", "supplied together"),
    ],
)
def test_transaction_listing_validates_pagination_filters_and_dates(client, query, message):
    response = client.get(f"/api/transactions?{query}")
    assert_error(response, 400, "invalid_query_parameter")
    assert message in response.get_json()["error"]["message"]


def test_transaction_listing_applies_supported_status_type_and_date_filters(client, app):
    insert_transactions(app, populated_entries())

    response = client.get(
        "/api/transactions?status=successful&transaction_type=deposit"
        "&start_date=2025-01-01&end_date=2025-01-02"
    )

    assert response.status_code == 200
    body = response.get_json()
    assert body["pagination"]["total_records"] == 1
    assert body["data"][0]["reference"] == "SYN-API-TX-0001"


def test_channel_filter_summary_analysis_and_csv_preserve_historical_unknown(client, app):
    insert_transactions(
        app,
        [
            ("deposit", "successful", "10.00", utc(1), "UPI"),
            ("withdrawal", "successful", "5.00", utc(1, 12), "ATM"),
            ("transfer", "pending", "2.00", utc(2), "BRANCH"),
            ("payment", "failed", "3.00", utc(2, 12)),
            ("deposit", "successful", "11.00", utc(3), "UPI"),
            ("withdrawal", "failed", "4.00", utc(3, 12)),
        ],
    )

    summary = client.get("/api/analytics/summary").get_json()["data"]
    analysis = client.get(
        "/api/analytics/volume?start_date=2025-01-01&end_date=2025-01-03"
    ).get_json()["data"]
    assert summary["counts_by_channel"] == {"UPI": 2, "ATM": 1, "BRANCH": 1, "UNKNOWN": 2}
    assert analysis["counts_by_channel"] == {"UPI": 1, "ATM": 1, "BRANCH": 1, "UNKNOWN": 1}

    upi = client.get("/api/transactions?channel=UPI&page_size=1").get_json()
    assert upi["pagination"]["total_records"] == 2
    assert upi["data"][0]["channel"] == "UPI"
    atm = client.get("/api/transactions?channel=ATM").get_json()
    assert atm["pagination"]["total_records"] == 1
    assert all(row["channel"] == "ATM" for row in atm["data"])
    branch = client.get("/api/transactions?channel=BRANCH").get_json()
    assert branch["pagination"]["total_records"] == 1
    assert all(row["channel"] == "BRANCH" for row in branch["data"])
    unknown = client.get("/api/transactions?channel=UNKNOWN").get_json()
    assert unknown["pagination"]["total_records"] == 2
    assert all(row["channel"] is None for row in unknown["data"])

    export = client.get(
        "/api/transactions/export?channel=UNKNOWN&start_date=2025-01-01&end_date=2025-01-04"
    )
    assert export.status_code == 200
    assert export.mimetype == "text/csv"
    assert export.headers["Content-Disposition"].endswith("transactions-not-recorded.csv")
    csv_text = export.get_data(as_text=True).lstrip("\ufeff")
    assert "Transaction reference,Occurred at (UTC),Type,Amount,Currency,Status,Channel,Source account,Destination account" in csv_text
    assert "SYN-API-TX-0004" in csv_text and "Not recorded" in csv_text
    assert "SYN-API-TX-0001" not in csv_text


def test_channel_export_requires_supported_channel(client):
    assert_error(client.get("/api/transactions/export?channel=POS"), 400, "invalid_query_parameter")


@pytest.mark.parametrize(
    ("export_format", "mimetype", "extension"),
    [
        ("csv", "text/csv", ".csv"),
        ("pdf", "application/pdf", ".pdf"),
        ("docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", ".docx"),
    ],
)
def test_exports_all_filtered_rows_with_account_details(client, app, export_format, mimetype, extension):
    insert_transactions(
        app,
        [
            ("deposit", "successful", "12.34", utc(1), "UPI"),
            ("payment", "pending", "8.00", utc(1, 12), "ATM"),
            ("withdrawal", "failed", "4.00", utc(2)),
        ],
    )
    query = (
        f"format={export_format}&status=successful&transaction_type=deposit&channel=UPI"
        "&start_date=2025-01-01&end_date=2025-01-02"
        "&account_number=SYN-API-ACCOUNT-0002"
    )

    response = client.get(f"/api/transactions/export?{query}")

    assert response.status_code == 200
    assert response.mimetype == mimetype
    assert response.headers["Content-Disposition"].endswith(extension)
    if export_format == "csv":
        records = list(csv.reader(io.StringIO(response.get_data(as_text=True).lstrip("\ufeff"))))
        assert records[0] == [
            "Transaction reference", "Occurred at (UTC)", "Type", "Amount", "Currency",
            "Status", "Channel", "Source account", "Destination account",
        ]
        assert len(records) == 2
        assert records[1][0:7] == ["SYN-API-TX-0001", "2025-01-01T00:00:00Z", "deposit", "12.34", "USD", "successful", "UPI"]
        assert records[1][8] == "SYN-API-ACCOUNT-0002"
    elif export_format == "pdf":
        payload = response.get_data()
        assert payload.startswith(b"%PDF-")
        assert b"Transaction Export" in payload
        assert b"Transactions: 1" in payload
        assert b"SYN-API-TX-0001" in payload
    else:
        with zipfile.ZipFile(io.BytesIO(response.get_data())) as archive:
            document_xml = archive.read("word/document.xml").decode("utf-8")
        assert "Transaction Export" in document_xml
        assert "Transactions: 1" in document_xml
        assert "SYN-API-TX-0001" in document_xml
        assert "SYN-API-ACCOUNT-0002" in document_xml


@pytest.mark.parametrize("export_format", ["csv", "pdf", "docx"])
def test_empty_exports_are_valid_and_include_zero_summary(client, export_format):
    response = client.get(f"/api/transactions/export?format={export_format}&channel=UNKNOWN")

    assert response.status_code == 200
    if export_format == "csv":
        records = list(csv.reader(io.StringIO(response.get_data(as_text=True).lstrip("\ufeff"))))
        assert len(records) == 1
        assert records[0][0] == "Transaction reference"
    elif export_format == "pdf":
        payload = response.get_data()
        assert payload.startswith(b"%PDF-")
        assert b"Transactions: 0" in payload
    else:
        with zipfile.ZipFile(io.BytesIO(response.get_data())) as archive:
            document_xml = archive.read("word/document.xml").decode("utf-8")
        assert "Transactions: 0" in document_xml


def test_csv_export_escapes_spreadsheet_formula_prefixes(client, app):
    insert_transactions(app, [("deposit", "successful", "1.00", utc(1), None)])
    with Session(app.extensions["database_engine"]) as session, session.begin():
        row = session.scalar(select(Transaction))
        row.reference = "=1+1"

    response = client.get("/api/transactions/export?format=csv")
    records = list(csv.reader(io.StringIO(response.get_data(as_text=True).lstrip("\ufeff"))))

    assert records[1][0] == "'=1+1"


def test_export_limit_rejects_excess_rows_without_partial_file(client, app, monkeypatch):
    insert_transactions(
        app,
        [("deposit", "successful", "1.00", utc(day)) for day in (1, 2)],
    )
    monkeypatch.setattr(importlib.import_module("app.api"), "MAX_EXPORT_ROWS", 1)

    response = client.get("/api/transactions/export?format=csv")

    assert_error(response, 413, "export_limit_exceeded")


def insert_account_search_transactions(app):
    """Create linked events for one synthetic account in both directions."""
    engine = app.extensions["database_engine"]
    with Session(engine) as session, session.begin():
        primary = Account(synthetic_reference="SYN-SEARCH-0001")
        other = Account(synthetic_reference="SYN-SEARCH-0002")
        session.add_all([primary, other])
        session.flush()
        entries = [
            ("SYN-SEARCH-TX-1", "deposit", "successful", "25.00", datetime(2025, 1, 1, tzinfo=timezone.utc), None, primary),
            ("SYN-SEARCH-TX-2", "withdrawal", "successful", "10.00", datetime(2025, 1, 2, tzinfo=timezone.utc), primary, None),
            ("SYN-SEARCH-TX-3", "transfer", "pending", "5.00", datetime(2025, 1, 3, tzinfo=timezone.utc), primary, other),
            ("SYN-SEARCH-TX-4", "payment", "successful", "8.00", datetime(2025, 1, 4, tzinfo=timezone.utc), primary, other),
            ("SYN-SEARCH-TX-5", "transfer", "successful", "7.00", datetime(2025, 1, 5, tzinfo=timezone.utc), other, primary),
            ("SYN-SEARCH-TX-6", "deposit", "failed", "12.00", datetime(2025, 1, 6, tzinfo=timezone.utc), None, primary),
            ("SYN-SEARCH-TX-CUTOFF", "withdrawal", "successful", "999.00", datetime(2026, 10, 19, tzinfo=timezone.utc), primary, None),
        ]
        for reference, transaction_type, status, amount, occurred_at, source, destination in entries:
            session.add(
                Transaction(
                    reference=reference,
                    source_account=source,
                    destination_account=destination,
                    transaction_type=transaction_type,
                    amount=Decimal(amount),
                    currency="USD",
                    occurred_at=occurred_at,
                    status=status,
                    description="Synthetic account search test event",
                )
            )


def test_account_transaction_search_returns_associated_events_masked_account_and_real_totals(client, app):
    insert_account_search_transactions(app)

    response = client.get(
        "/api/accounts/transactions?account_number=syn-search-0001&page_size=2"
    )

    assert response.status_code == 200
    body = response.get_json()
    assert body["account"] == {"account_number_masked": "••••0001"}
    assert body["summary"] == {
        "transaction_count": 6,
        "successful_debit_total": "18.00",
        "successful_credit_total": "32.00",
    }
    assert body["pagination"] == {
        "page": 1,
        "page_size": 2,
        "total_records": 6,
        "total_pages": 3,
        "has_previous": False,
        "has_next": True,
    }
    assert [row["reference"] for row in body["data"]] == [
        "SYN-SEARCH-TX-6",
        "SYN-SEARCH-TX-5",
    ]
    assert all(row["id"] and row["currency"] == "USD" for row in body["data"])
    assert "SYN-SEARCH-0001" not in response.get_data(as_text=True)


def test_account_transaction_search_applies_date_filters_and_pagination_totals(client, app):
    insert_account_search_transactions(app)

    response = client.get(
        "/api/accounts/transactions?account_number=SYN-SEARCH-0001"
        "&start_date=2025-01-02&end_date=2025-01-05&page_size=1&page=2"
    )

    body = response.get_json()
    assert response.status_code == 200
    assert body["summary"] == {
        "transaction_count": 3,
        "successful_debit_total": "18.00",
        "successful_credit_total": "0.00",
    }
    assert body["pagination"]["total_records"] == 3
    assert body["pagination"]["total_pages"] == 3
    assert body["pagination"]["page"] == 2
    assert body["data"][0]["reference"] == "SYN-SEARCH-TX-3"


def test_account_transaction_search_supports_channel_filter(client, app):
    insert_account_search_transactions(app)

    body = client.get(
        "/api/accounts/transactions?account_number=SYN-SEARCH-0001&channel=UNKNOWN"
    ).get_json()

    assert body["pagination"]["total_records"] == 6
    assert all(row["channel"] is None for row in body["data"])


def test_account_transaction_search_obeys_demo_cutoff_even_for_date_filters(client, app):
    insert_account_search_transactions(app)

    response = client.get(
        "/api/accounts/transactions?account_number=SYN-SEARCH-0001"
        "&start_date=2026-10-19&end_date=2026-10-20"
    )

    assert response.status_code == 200
    assert response.get_json()["summary"] == {
        "transaction_count": 0,
        "successful_debit_total": "0.00",
        "successful_credit_total": "0.00",
    }
    assert response.get_json()["data"] == []


def test_account_search_distinguishes_unknown_account_from_known_empty_account(client, app):
    with Session(app.extensions["database_engine"]) as session, session.begin():
        session.add(Account(synthetic_reference="SYN-EMPTY-0001"))

    unknown = client.get("/api/accounts/transactions?account_number=SYN-MISSING-0001")
    known_empty = client.get("/api/accounts/transactions?account_number=SYN-EMPTY-0001")

    assert_error(unknown, 404, "account_not_found")
    assert known_empty.status_code == 200
    assert known_empty.get_json()["summary"] == {
        "transaction_count": 0,
        "successful_debit_total": "0.00",
        "successful_credit_total": "0.00",
    }
    assert known_empty.get_json()["data"] == []


@pytest.mark.parametrize(
    ("query", "code"),
    [
        ("", "invalid_account_number"),
        ("account_number=", "invalid_account_number"),
        ("account_number=bad%2Fvalue", "invalid_account_number"),
        ("account_number=%27%20OR%201%3D1%20--", "invalid_account_number"),
    ],
)
def test_account_search_rejects_missing_invalid_and_injection_style_values(client, query, code):
    response = client.get(f"/api/accounts/transactions?{query}")

    assert_error(response, 400, code)


def test_errors_are_json_when_authentication_configuration_is_unavailable():
    app_without_database = create_app({"TESTING": True, "DATABASE_URL": None})
    client = app_without_database.test_client()

    assert_error(client.get("/api/analytics/summary"), 503, "authentication_unavailable")
    assert_error(
        client.get("/api/accounts/transactions?account_number=SYN-ACCT-0001"),
        503,
        "authentication_unavailable",
    )
    assert_error(client.get("/api/not-a-route"), 404, "not_found")
    assert_error(client.get("/api/analytics/summary?unexpected=value"), 503, "authentication_unavailable")
