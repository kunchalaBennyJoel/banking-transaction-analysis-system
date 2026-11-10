"""Read-only JSON endpoints for transaction analytics and browsing."""

import re
from io import BytesIO
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

from flask import Blueprint, current_app, jsonify, request, send_file
from sqlalchemy import case, func, or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, selectinload
from werkzeug.exceptions import HTTPException

from .analytics import summarize_transactions, transaction_cutoff_filter
from .exporters import render_csv, render_docx, render_pdf
from .models import Account, Transaction

api = Blueprint("api", __name__, url_prefix="/api")

SUPPORTED_STATUSES = {"successful", "pending", "failed"}
SUPPORTED_TYPES = {"deposit", "withdrawal", "transfer", "payment"}
SUPPORTED_CHANNELS = {"UPI", "ATM", "BRANCH", "UNKNOWN"}
DEFAULT_PAGE_SIZE = 25
MAX_PAGE_SIZE = 100
MAX_EXPORT_ROWS = 10_000


class ApiError(Exception):
    """An expected API error safe to return to a client."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def _error_response(status: int, code: str, message: str):
    return jsonify(error={"code": code, "message": message}), status


@api.app_errorhandler(ApiError)
def handle_api_error(error: ApiError):
    return _error_response(error.status, error.code, error.message)


@api.app_errorhandler(SQLAlchemyError)
def handle_database_error(error: SQLAlchemyError):
    current_app.logger.exception("Database API request failed")
    return _error_response(503, "database_unavailable", "The database request could not be completed.")


@api.app_errorhandler(HTTPException)
def handle_http_error(error: HTTPException):
    if error.code == 404:
        return _error_response(404, "not_found", "The requested resource was not found.")
    if error.code == 405:
        return _error_response(405, "method_not_allowed", "The method is not allowed for this resource.")
    return _error_response(error.code or 500, "http_error", "The request could not be completed.")


@api.app_errorhandler(Exception)
def handle_unexpected_error(error: Exception):
    current_app.logger.exception("Unexpected API request failure")
    return _error_response(500, "internal_error", "The request could not be completed.")


def _database_engine():
    engine = current_app.extensions.get("database_engine")
    if engine is None:
        raise ApiError(503, "database_unavailable", "Database configuration is not available.")
    return engine


def _validate_query_names(allowed: set[str]) -> None:
    unknown = set(request.args.keys()) - allowed
    if unknown:
        name = sorted(unknown)[0]
        raise ApiError(400, "invalid_query_parameter", f"Unsupported query parameter: {name}.")


def _query_value(name: str) -> str | None:
    values = request.args.getlist(name)
    if len(values) > 1:
        raise ApiError(400, "invalid_query_parameter", f"Query parameter {name} must appear once.")
    return values[0] if values else None


def _parse_date(value: str | None, name: str) -> date | None:
    if value is None:
        return None
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ApiError(400, "invalid_query_parameter", f"{name} must use YYYY-MM-DD format.")
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise ApiError(400, "invalid_query_parameter", f"{name} is not a valid calendar date.") from error


def _parse_date_window(*, required: bool) -> tuple[datetime, datetime] | None:
    start_date = _parse_date(_query_value("start_date"), "start_date")
    end_date = _parse_date(_query_value("end_date"), "end_date")
    if start_date is None and end_date is None and not required:
        return None
    if start_date is None or end_date is None:
        raise ApiError(400, "invalid_query_parameter", "start_date and end_date must be supplied together.")
    if start_date >= end_date:
        raise ApiError(400, "invalid_query_parameter", "start_date must be earlier than the exclusive end_date.")

    # Date-only API boundaries map to midnight UTC; the end remains exclusive.
    start_at = datetime.combine(start_date, time.min, tzinfo=timezone.utc).replace(tzinfo=None)
    end_at = datetime.combine(end_date, time.min, tzinfo=timezone.utc).replace(tzinfo=None)
    return start_at, end_at


def _parse_positive_integer(name: str, default: int, maximum: int | None = None) -> int:
    raw_value = _query_value(name)
    if raw_value is None:
        return default
    if not re.fullmatch(r"[0-9]+", raw_value):
        raise ApiError(400, "invalid_query_parameter", f"{name} must be a positive integer.")
    value = int(raw_value)
    if value < 1:
        raise ApiError(400, "invalid_query_parameter", f"{name} must be a positive integer.")
    if maximum is not None and value > maximum:
        raise ApiError(400, "invalid_query_parameter", f"{name} must not exceed {maximum}.")
    return value


def _decimal_string(amount: Decimal) -> str:
    """Serialize an exact two-decimal monetary value without converting to float."""
    return format(amount, ".2f")


def _validate_transaction_filters(
    allowed: set[str],
) -> tuple[int, int, str | None, str | None, str | None, tuple[datetime, datetime] | None]:
    """Parse shared transaction-list filters once for both listing endpoints."""
    _validate_query_names(allowed)
    page = _parse_positive_integer("page", 1)
    page_size = _parse_positive_integer("page_size", DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE)
    status = _query_value("status")
    if status is not None and status not in SUPPORTED_STATUSES:
        allowed_values = ", ".join(sorted(SUPPORTED_STATUSES))
        raise ApiError(
            400, "invalid_query_parameter", f"status must be one of: {allowed_values}."
        )
    transaction_type = _query_value("transaction_type")
    if transaction_type is not None and transaction_type not in SUPPORTED_TYPES:
        allowed_values = ", ".join(sorted(SUPPORTED_TYPES))
        raise ApiError(
            400,
            "invalid_query_parameter",
            f"transaction_type must be one of: {allowed_values}.",
        )
    channel = _query_value("channel")
    if channel is not None and channel not in SUPPORTED_CHANNELS:
        allowed_values = ", ".join(sorted(SUPPORTED_CHANNELS))
        raise ApiError(400, "invalid_query_parameter", f"channel must be one of: {allowed_values}.")
    window = _parse_date_window(required=False)
    return page, page_size, status, transaction_type, channel, window


def _transaction_filters(status, transaction_type, channel, window):
    filters = [transaction_cutoff_filter()]
    if status is not None:
        filters.append(Transaction.status == status)
    if transaction_type is not None:
        filters.append(Transaction.transaction_type == transaction_type)
    if channel == "UNKNOWN":
        filters.append(Transaction.channel.is_(None))
    elif channel is not None:
        filters.append(Transaction.channel == channel)
    if window is not None:
        start_at, end_at = window
        filters.extend(
            [Transaction.occurred_at >= start_at, Transaction.occurred_at < end_at]
        )
    return filters


def _transaction_json(row: Transaction) -> dict:
    return {
        "id": row.id,
        "reference": row.reference,
        "source_account_id": row.source_account_id,
        "destination_account_id": row.destination_account_id,
        "transaction_type": row.transaction_type,
        "amount": _decimal_string(row.amount),
        "currency": row.currency,
        "occurred_at": _utc_timestamp(row.occurred_at),
        "status": row.status,
        "description": row.description,
        "channel": row.channel,
    }


def _pagination_json(page: int, page_size: int, total_records: int) -> dict:
    total_pages = (total_records + page_size - 1) // page_size
    return {
        "page": page,
        "page_size": page_size,
        "total_records": total_records,
        "total_pages": total_pages,
        "has_previous": page > 1,
        "has_next": page < total_pages,
    }


def _utc_timestamp(value: datetime) -> str:
    """Serialize stored UTC timestamps consistently, including SQLite naive values."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    else:
        value = value.astimezone(timezone.utc)
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _daily_volume(summary, *, fill_gaps: bool = False) -> list[dict[str, int | str]]:
    counts = {day.day: day.transaction_count for day in summary.daily_volume}
    if fill_gaps and counts:
        current_day = min(counts)
        last_day = max(counts)
        daily = []
        while current_day <= last_day:
            daily.append({"date": current_day.isoformat(), "transaction_count": counts.get(current_day, 0)})
            current_day += timedelta(days=1)
        return daily
    return [
        {"date": day.isoformat(), "transaction_count": count}
        for day, count in sorted(counts.items())
    ]


def _summary_metrics(summary) -> dict:
    """Serialize values already calculated by the shared analytics service."""
    return {
        "currency": summary.currency,
        "total_transaction_count": summary.total_transaction_count,
        "successful_transaction_count": summary.successful_transaction_count,
        "successful_transaction_value": _decimal_string(summary.successful_transaction_value),
        "average_successful_transaction_amount": (
            _decimal_string(summary.average_successful_transaction_amount)
            if summary.average_successful_transaction_amount is not None
            else None
        ),
        "counts_by_status": summary.counts_by_status,
        "counts_by_type": summary.counts_by_type,
        "counts_by_channel": summary.counts_by_channel,
        "successful_deposit_total": _decimal_string(summary.successful_deposit_total),
        "successful_withdrawal_total": _decimal_string(summary.successful_withdrawal_total),
    }


@api.get("/analytics/summary")
def analytics_summary():
    _validate_query_names(set())
    engine = _database_engine()
    with Session(engine) as session:
        try:
            summary = summarize_transactions(session)
        except ValueError as error:
            raise ApiError(
                503,
                "analytics_unavailable",
                "Analytics cannot be calculated for the current currency data.",
            ) from error

    return jsonify(data={**_summary_metrics(summary), "daily_volume": _daily_volume(summary, fill_gaps=True)})


@api.get("/analytics/volume")
def analytics_volume():
    _validate_query_names({"start_date", "end_date"})
    window = _parse_date_window(required=True)
    assert window is not None
    start_at, end_at = window
    # The Stage C service requires aware bounds and normalizes them to UTC.
    start_aware = start_at.replace(tzinfo=timezone.utc)
    end_aware = end_at.replace(tzinfo=timezone.utc)
    engine = _database_engine()
    with Session(engine) as session:
        try:
            summary = summarize_transactions(session, start_at=start_aware, end_at=end_aware)
        except ValueError as error:
            raise ApiError(
                503,
                "analytics_unavailable",
                "Analytics cannot be calculated for the current currency data.",
            ) from error

    return jsonify(
        data={
            "start_date_inclusive": start_at.date().isoformat(),
            "end_date_exclusive": end_at.date().isoformat(),
            "timezone": "UTC",
            **_summary_metrics(summary),
            "daily_volume": _daily_volume(summary),
        }
    )


@api.get("/transactions")
def list_transactions():
    allowed = {"page", "page_size", "status", "transaction_type", "channel", "start_date", "end_date"}
    page, page_size, status, transaction_type, channel, window = _validate_transaction_filters(
        allowed
    )
    filters = _transaction_filters(status, transaction_type, channel, window)

    engine = _database_engine()
    with Session(engine) as session:
        total_records = session.scalar(
            select(func.count(Transaction.id)).where(*filters)
        ) or 0
        rows = session.scalars(
            select(Transaction)
            .where(*filters)
            .order_by(Transaction.occurred_at.desc(), Transaction.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).all()

    return jsonify(
        data=[_transaction_json(row) for row in rows],
        pagination=_pagination_json(page, page_size, total_records),
    )


@api.get("/transactions/export")
def export_transactions():
    """Download all matching transactions in a bounded CSV, PDF, or Word report."""
    allowed = {
        "format", "channel", "status", "transaction_type", "start_date", "end_date",
        "account_number",
    }
    _validate_query_names(allowed)
    _, _, status, transaction_type, channel, window = _validate_transaction_filters(allowed)
    filters = _transaction_filters(status, transaction_type, channel, window)

    export_format = _query_value("format") or "csv"
    if export_format not in {"csv", "pdf", "docx"}:
        raise ApiError(400, "invalid_query_parameter", "format must be one of: csv, pdf, docx.")
    account_number = _query_value("account_number")
    if account_number is not None:
        account_number = account_number.strip().upper()
        if not re.fullmatch(r"[A-Z0-9_-]{1,24}", account_number):
            raise ApiError(400, "invalid_account_number", "Enter a valid synthetic account number.")

    engine = _database_engine()
    with Session(engine) as session:
        if account_number is not None:
            account = session.scalar(
                select(Account).where(func.upper(Account.synthetic_reference) == account_number)
            )
            if account is None:
                raise ApiError(404, "account_not_found", "No synthetic account matched that account number.")
            filters.append(or_(
                Transaction.source_account_id == account.id,
                Transaction.destination_account_id == account.id,
            ))

        total_records = session.scalar(select(func.count(Transaction.id)).where(*filters)) or 0
        if total_records > MAX_EXPORT_ROWS:
            raise ApiError(
                413,
                "export_limit_exceeded",
                f"This export contains more than {MAX_EXPORT_ROWS:,} transactions. Narrow the filters and try again.",
            )
        rows = session.scalars(
            select(Transaction)
            .options(
                selectinload(Transaction.source_account),
                selectinload(Transaction.destination_account),
            )
            .where(*filters)
            .order_by(Transaction.occurred_at, Transaction.id)
            .limit(MAX_EXPORT_ROWS + 1)
        ).all()

    filters_for_report = []
    if account_number:
        filters_for_report.append(f"account={account_number}")
    if status:
        filters_for_report.append(f"status={status}")
    if transaction_type:
        filters_for_report.append(f"type={transaction_type}")
    if channel:
        filters_for_report.append(f"channel={channel.title() if channel != 'UNKNOWN' else 'Not recorded'}")
    if window:
        filters_for_report.append(f"UTC {window[0].date().isoformat()} to before {window[1].date().isoformat()}")
    filter_summary = "; ".join(filters_for_report)

    if export_format == "csv":
        content = render_csv(rows)
        mimetype = "text/csv; charset=utf-8"
        extension = "csv"
    elif export_format == "pdf":
        content = render_pdf(rows, filter_summary)
        mimetype = "application/pdf"
        extension = "pdf"
    else:
        content = render_docx(rows, filter_summary)
        mimetype = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        extension = "docx"

    channel_slug = "not-recorded" if channel == "UNKNOWN" else channel.lower() if channel else None
    response = send_file(
        BytesIO(content),
        mimetype=mimetype,
        as_attachment=True,
        download_name=(
            f"transactions-{channel_slug}.{extension}" if channel_slug else f"transactions.{extension}"
        ),
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@api.get("/accounts/transactions")
def account_transactions():
    """Search paginated synthetic transactions and totals for one account."""
    allowed = {
        "account_number", "page", "page_size", "status", "transaction_type",
        "start_date", "end_date",
        "channel",
    }
    _validate_query_names(allowed)
    account_number = _query_value("account_number")
    if account_number is None:
        raise ApiError(400, "invalid_account_number", "Enter a synthetic account number to search.")
    account_number = account_number.strip().upper()
    if not re.fullmatch(r"[A-Z0-9_-]{1,24}", account_number):
        raise ApiError(400, "invalid_account_number", "Enter a valid synthetic account number.")

    page, page_size, status, transaction_type, channel, window = _validate_transaction_filters(allowed)
    engine = _database_engine()
    with Session(engine) as session:
        account = session.scalar(
            select(Account).where(func.upper(Account.synthetic_reference) == account_number)
        )
        if account is None:
            raise ApiError(
                404,
                "account_not_found",
                "No synthetic account matched that account number.",
            )

        filters = _transaction_filters(status, transaction_type, channel, window)
        account_membership = or_(
            Transaction.source_account_id == account.id,
            Transaction.destination_account_id == account.id,
        )
        account_filters = [*filters, account_membership]
        total_records = session.scalar(
            select(func.count(Transaction.id)).where(*account_filters)
        ) or 0

        successful = Transaction.status == "successful"
        debit_total, credit_total = session.execute(
            select(
                func.coalesce(
                    func.sum(
                        case(
                            (
                                successful & (Transaction.source_account_id == account.id),
                                Transaction.amount,
                            ),
                            else_=0,
                        )
                    ),
                    0,
                ),
                func.coalesce(
                    func.sum(
                        case(
                            (
                                successful
                                & (Transaction.destination_account_id == account.id),
                                Transaction.amount,
                            ),
                            else_=0,
                        )
                    ),
                    0,
                ),
            ).where(*account_filters)
        ).one()
        rows = session.scalars(
            select(Transaction)
            .where(*account_filters)
            .order_by(Transaction.occurred_at.desc(), Transaction.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).all()

        # The only account identifiers in this demo are synthetic references.
        # Still mask them in responses so the endpoint does not echo a full identifier.
        masked_number = f"••••{account.synthetic_reference[-4:]}"

    return jsonify(
        account={"account_number_masked": masked_number},
        summary={
            "transaction_count": total_records,
            "successful_debit_total": _decimal_string(debit_total),
            "successful_credit_total": _decimal_string(credit_total),
        },
        data=[_transaction_json(row) for row in rows],
        pagination=_pagination_json(page, page_size, total_records),
    )
