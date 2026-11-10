"""Relational models for synthetic account and transaction records."""

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


class Account(Base):
    """Synthetic account reference with no customer or personally identifying fields."""

    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    synthetic_reference: Mapped[str] = mapped_column(String(24), nullable=False, unique=True)

    outgoing_transactions: Mapped[list["Transaction"]] = relationship(
        back_populates="source_account",
        foreign_keys="Transaction.source_account_id",
    )
    incoming_transactions: Mapped[list["Transaction"]] = relationship(
        back_populates="destination_account",
        foreign_keys="Transaction.destination_account_id",
    )


class Employee(Base):
    """Application identity, separate from the synthetic banking accounts."""

    __tablename__ = "employees"
    __table_args__ = (
        UniqueConstraint("employee_id", name="uq_employees_employee_id"),
        CheckConstraint("role IN ('admin', 'employee')", name="ck_employees_role"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    employee_id: Mapped[str] = mapped_column(String(64), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(10), nullable=False, default="employee")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )

    def __repr__(self) -> str:
        return f"Employee(id={self.id!r}, employee_id={self.employee_id!r}, role={self.role!r})"


class Transaction(Base):
    """A single transaction event; a transfer is stored once with two account links."""

    __tablename__ = "transactions"
    __table_args__ = (
        UniqueConstraint("reference", name="uq_transactions_reference"),
        CheckConstraint("amount > 0", name="ck_transactions_amount_positive"),
        CheckConstraint(
            "transaction_type IN ('deposit', 'withdrawal', 'transfer', 'payment')",
            name="ck_transactions_type",
        ),
        CheckConstraint(
            "status IN ('successful', 'pending', 'failed')",
            name="ck_transactions_status",
        ),
        CheckConstraint(
            "channel IS NULL OR channel IN ('UPI', 'ATM', 'BRANCH')",
            name="ck_transactions_channel",
        ),
        CheckConstraint(
            "(transaction_type = 'deposit' AND source_account_id IS NULL AND destination_account_id IS NOT NULL) OR "
            "(transaction_type = 'withdrawal' AND source_account_id IS NOT NULL AND destination_account_id IS NULL) OR "
            "(transaction_type IN ('transfer', 'payment') AND source_account_id IS NOT NULL "
            "AND destination_account_id IS NOT NULL AND source_account_id <> destination_account_id)",
            name="ck_transactions_account_movement",
        ),
        CheckConstraint("length(currency) = 3", name="ck_transactions_currency_length"),
        Index("ix_transactions_occurred_at", "occurred_at"),
        Index("ix_transactions_status_occurred_at", "status", "occurred_at"),
        Index("ix_transactions_type_occurred_at", "transaction_type", "occurred_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    reference: Mapped[str] = mapped_column(String(40), nullable=False)
    source_account_id: Mapped[int | None] = mapped_column(
        ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=True
    )
    destination_account_id: Mapped[int | None] = mapped_column(
        ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=True
    )
    transaction_type: Mapped[str] = mapped_column(String(16), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="USD")
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(12), nullable=False)
    description: Mapped[str | None] = mapped_column(String(120), nullable=True)
    channel: Mapped[str | None] = mapped_column(String(10), nullable=True)

    source_account: Mapped[Account | None] = relationship(
        back_populates="outgoing_transactions",
        foreign_keys=[source_account_id],
    )
    destination_account: Mapped[Account | None] = relationship(
        back_populates="incoming_transactions",
        foreign_keys=[destination_account_id],
    )
