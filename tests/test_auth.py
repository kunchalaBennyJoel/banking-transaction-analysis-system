import re
import secrets

import pytest
from sqlalchemy import inspect, select
from sqlalchemy.orm import Session
from werkzeug.security import check_password_hash, generate_password_hash

from app import create_app
from app.database import create_schema
from app.models import Employee


@pytest.fixture
def app():
    application = create_app(
        {
            "TESTING": True,
            "APP_ENV": "development",
            "DATABASE_URL": "sqlite+pysqlite:///:memory:",
            "SECRET_KEY": secrets.token_hex(32),
        }
    )
    create_schema(application.extensions["database_engine"])
    with Session(application.extensions["database_engine"]) as db_session, db_session.begin():
        admin = Employee(
            employee_id="AUTH-ADMIN",
            password_hash=generate_password_hash("CorrectHorseBattery1!"),
            role="admin",
            is_active=True,
        )
        employee = Employee(
            employee_id="AUTH-EMPLOYEE",
            password_hash=generate_password_hash("CorrectHorseBattery2!"),
            role="employee",
            is_active=True,
        )
        inactive = Employee(
            employee_id="AUTH-INACTIVE",
            password_hash=generate_password_hash("CorrectHorseBattery3!"),
            role="employee",
            is_active=False,
        )
        db_session.add_all([admin, employee, inactive])
        db_session.flush()
        application.extensions["auth_test_ids"] = {
            "admin": admin.id,
            "employee": employee.id,
            "inactive": inactive.id,
        }
    yield application
    application.extensions["database_engine"].dispose()


@pytest.fixture
def client(app):
    return app.test_client()


def csrf_token(client, path="/login"):
    response = client.get(path)
    assert response.status_code == 200
    match = re.search(r'name="csrf_token" value="([^"]+)"', response.get_data(as_text=True))
    assert match is not None
    return match.group(1)


def login(client, employee_id="AUTH-ADMIN", password="CorrectHorseBattery1!", next_url=None):
    token = csrf_token(client)
    response = client.post(
        "/login",
        data={"csrf_token": token, "employee_id": employee_id, "password": password, "next": next_url or ""},
    )
    return response


def test_health_is_public_and_private_pages_redirect_to_login(client):
    health = client.get("/health")
    overview = client.get("/")
    transactions = client.get("/transactions")

    assert health.status_code == 200
    assert health.get_json() == {"status": "ok"}
    assert overview.status_code == 302
    assert overview.headers["Location"].startswith("/login?")
    assert transactions.status_code == 302
    assert transactions.headers["Location"].startswith("/login?")


@pytest.mark.parametrize(
    "path",
    [
        "/api/transactions",
        "/api/transactions/export?channel=UPI",
        "/api/transactions/export?format=pdf",
        "/api/transactions/export?format=docx",
        "/api/accounts/transactions?account_number=SYN-ACCT-0001",
        "/api/analytics/summary",
        "/api/analytics/volume?start_date=2025-01-01&end_date=2025-02-01",
    ],
)
def test_all_api_endpoints_return_json_401_without_authentication(client, path):
    response = client.get(path)

    assert response.status_code == 401
    assert response.get_json() == {
        "error": {
            "code": "authentication_required",
            "message": "Log in to access this resource.",
        }
    }


def test_login_authenticates_employee_and_stores_only_session_identity(client):
    response = login(client, next_url="/analysis")

    assert response.status_code == 302
    assert response.headers["Location"] == "/analysis"
    with client.session_transaction() as flask_session:
        assert flask_session["employee_pk"]
        assert flask_session.permanent is True
        assert "password" not in flask_session
        assert "password_hash" not in flask_session


@pytest.mark.parametrize(
    ("employee_id", "password"),
    [
        ("AUTH-ADMIN", "wrong password"),
        ("AUTH-UNKNOWN", "CorrectHorseBattery1!"),
        ("AUTH-INACTIVE", "CorrectHorseBattery3!"),
        ("", ""),
    ],
)
def test_invalid_or_inactive_login_is_rejected_without_account_enumeration(client, employee_id, password):
    response = login(client, employee_id=employee_id, password=password)

    assert response.status_code == 200
    assert "Employee ID or password is incorrect." in response.get_data(as_text=True)
    assert "AUTH-UNKNOWN" not in response.get_data(as_text=True)


def test_login_rejects_missing_csrf_token(client):
    response = client.post(
        "/login", data={"employee_id": "AUTH-ADMIN", "password": "CorrectHorseBattery1!"}
    )

    assert response.status_code == 400


def test_login_does_not_redirect_to_external_next_url(client):
    response = login(client, next_url="https://attacker.example/steal")

    assert response.status_code == 302
    assert response.headers["Location"] == "/"


def test_logout_requires_csrf_and_clears_session(client):
    assert login(client).status_code == 302
    token = csrf_token(client, "/transactions")
    response = client.post("/logout", data={"csrf_token": token})

    assert response.status_code == 302
    assert response.headers["Location"] == "/login"
    assert client.get("/api/transactions").status_code == 401


def test_employee_cannot_access_admin_management(client):
    response = login(
        client,
        employee_id="AUTH-EMPLOYEE",
        password="CorrectHorseBattery2!",
    )

    assert response.status_code == 302
    assert client.get("/admin/employees").status_code == 403


def test_admin_can_create_employee_with_only_a_password_hash_stored(app, client):
    assert login(client).status_code == 302
    response = client.get("/admin/employees")
    token = re.search(r'name="csrf_token" value="([^"]+)"', response.get_data(as_text=True)).group(1)
    password = "A-new-example-password-4!"

    created = client.post(
        "/admin/employees",
        data={
            "csrf_token": token,
            "employee_id": "NEW-EMPLOYEE",
            "password": password,
            "role": "employee",
        },
    )

    assert created.status_code == 200
    assert password not in created.get_data(as_text=True)
    with Session(app.extensions["database_engine"]) as db_session:
        employee = db_session.scalar(
            select(Employee).where(Employee.employee_id == "NEW-EMPLOYEE")
        )
        assert employee is not None
        assert employee.role == "employee"
        assert employee.is_active is True
        assert employee.password_hash != password
        assert check_password_hash(employee.password_hash, password)


def test_admin_can_deactivate_another_account_and_the_session_is_revoked(app, client):
    assert login(client).status_code == 302
    target_id = app.extensions["auth_test_ids"]["employee"]
    response = client.get("/admin/employees")
    token = re.search(r'name="csrf_token" value="([^"]+)"', response.get_data(as_text=True)).group(1)

    deactivated = client.post(
        f"/admin/employees/{target_id}/deactivate", data={"csrf_token": token}
    )

    assert deactivated.status_code == 302
    with Session(app.extensions["database_engine"]) as db_session:
        assert db_session.get(Employee, target_id).is_active is False

    other_client = app.test_client()
    with other_client.session_transaction() as flask_session:
        flask_session["employee_pk"] = target_id
    assert other_client.get("/transactions").status_code == 302


def test_admin_cannot_deactivate_itself_or_the_last_active_admin(app, client):
    assert login(client).status_code == 302
    admin_id = app.extensions["auth_test_ids"]["admin"]
    response = client.get("/admin/employees")
    token = re.search(r'name="csrf_token" value="([^"]+)"', response.get_data(as_text=True)).group(1)

    result = client.post(f"/admin/employees/{admin_id}/deactivate", data={"csrf_token": token})

    assert result.status_code == 302
    with Session(app.extensions["database_engine"]) as db_session:
        assert db_session.get(Employee, admin_id).is_active is True


def test_employee_table_cli_creates_only_the_authentication_table(app):
    local_app = create_app(
        {
            "TESTING": True,
            "APP_ENV": "development",
            "DATABASE_URL": "sqlite+pysqlite:///:memory:",
            "SECRET_KEY": secrets.token_hex(32),
        }
    )
    engine = local_app.extensions["database_engine"]

    result = local_app.test_cli_runner().invoke(args=["init-employee-table"])

    assert result.exit_code == 0
    assert inspect(engine).get_table_names() == ["employees"]
    engine.dispose()


def test_initial_admin_cli_prompts_for_password_and_stores_hash(app):
    with Session(app.extensions["database_engine"]) as db_session, db_session.begin():
        db_session.execute(Employee.__table__.delete())
    password = "Initial-admin-password-example!"

    result = app.test_cli_runner().invoke(
        args=["create-admin", "--employee-id", "INITIAL-ADMIN"],
        input=f"{password}\n{password}\n",
    )

    assert result.exit_code == 0
    assert password not in result.output
    with Session(app.extensions["database_engine"]) as db_session:
        employee = db_session.scalar(
            select(Employee).where(Employee.employee_id == "INITIAL-ADMIN")
        )
        assert employee is not None
        assert employee.role == "admin"
        assert employee.password_hash != password
        assert check_password_hash(employee.password_hash, password)


def test_production_requires_secret_key_and_uses_secure_cookie_settings():
    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        create_app(
            {
                "ENVIRONMENT": "production",
                "SECRET_KEY": None,
                "DATABASE_URL": None,
            }
        )

    app = create_app(
        {
            "ENVIRONMENT": "production",
            "SECRET_KEY": secrets.token_hex(32),
            "DATABASE_URL": None,
        }
    )
    assert app.config["SESSION_COOKIE_SECURE"] is True
    assert app.config["SESSION_COOKIE_HTTPONLY"] is True
    assert app.config["SESSION_COOKIE_SAMESITE"] == "Lax"
