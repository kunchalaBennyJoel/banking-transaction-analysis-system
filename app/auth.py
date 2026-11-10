"""Employee authentication, authorization, and account administration."""

import re
from functools import wraps
from urllib.parse import urlsplit

import click
from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask_wtf.csrf import CSRFError, CSRFProtect
from sqlalchemy import func, inspect, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session
from werkzeug.security import check_password_hash, generate_password_hash

from .models import Employee

auth = Blueprint("auth", __name__)
csrf = CSRFProtect()
EMPLOYEE_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{2,63}\Z")
MIN_PASSWORD_LENGTH = 12


def _api_error(status: int, code: str, message: str):
    return jsonify(error={"code": code, "message": message}), status


def _safe_next_url(target: str | None) -> str | None:
    if not target:
        return None
    parsed = urlsplit(target)
    if parsed.scheme or parsed.netloc or not target.startswith("/") or target.startswith("//"):
        return None
    return target


def _employee_engine():
    engine = current_app.extensions.get("database_engine")
    if engine is None:
        raise RuntimeError("Database configuration is not available.")
    return engine


def _current_employee() -> Employee | None:
    employee_id = session.get("employee_pk")
    if employee_id is None:
        return None
    with Session(_employee_engine()) as db_session:
        employee = db_session.get(Employee, employee_id)
        if employee is None or not employee.is_active:
            session.clear()
            return None
        return employee


def _valid_employee_id(value: str) -> bool:
    return bool(EMPLOYEE_ID_PATTERN.fullmatch(value))


def _valid_password(value: str) -> bool:
    return MIN_PASSWORD_LENGTH <= len(value) <= 1024


def _database_error_response(error: SQLAlchemyError):
    current_app.logger.exception("Authentication database request failed")
    if request.path.startswith("/api/"):
        return _api_error(503, "database_unavailable", "The database request could not be completed.")
    return render_template(
        "service_error.html",
        active_page="service-error",
        message="Employee authentication is temporarily unavailable.",
    ), 503


def init_auth(app):
    """Enable CSRF, load active employees, and protect private routes."""
    @app.context_processor
    def employee_context():
        return {"current_employee": getattr(g, "current_employee", None)}

    @app.before_request
    def authenticate_request():
        g.current_employee = None
        if request.path == "/health" or request.endpoint == "static":
            return None
        if request.endpoint is None:
            return None

        if not current_app.secret_key:
            if request.endpoint == "auth.login":
                return None
            if request.path.startswith("/api/"):
                return _api_error(503, "authentication_unavailable", "Employee authentication is not configured.")
            return render_template(
                "service_error.html",
                active_page="service-error",
                message="Employee authentication is not configured.",
            ), 503

        try:
            employee = _current_employee()
        except RuntimeError:
            if request.path.startswith("/api/"):
                return _api_error(503, "database_unavailable", "Database configuration is not available.")
            return render_template(
                "service_error.html",
                active_page="service-error",
                message="Employee authentication is temporarily unavailable.",
            ), 503
        except SQLAlchemyError as error:
            return _database_error_response(error)

        g.current_employee = employee
        if employee is not None:
            if request.endpoint == "auth.login":
                return redirect(url_for("overview"))
            return None

        if request.endpoint == "auth.login" or request.endpoint is None:
            return None
        if request.path.startswith("/api/"):
            return _api_error(401, "authentication_required", "Log in to access this resource.")
        return redirect(url_for("auth.login", next=request.full_path.rstrip("?")))

    # Run authentication first so unauthenticated API calls consistently get JSON 401.
    csrf.init_app(app)

    @app.errorhandler(SQLAlchemyError)
    def handle_auth_database_error(error: SQLAlchemyError):
        return _database_error_response(error)

    @app.errorhandler(CSRFError)
    def handle_csrf_error(error: CSRFError):
        del error
        if request.path.startswith("/api/"):
            return _api_error(400, "csrf_failed", "The request token is missing or invalid.")
        return render_template(
            "service_error.html",
            active_page="service-error",
            message="The form expired or its security token was invalid. Reload and try again.",
        ), 400

    @app.cli.command("init-employee-table")
    def init_employee_table():
        """Create only the Employee table in a local SQLite or localhost MySQL database."""
        engine = app.extensions.get("database_engine")
        if engine is None:
            raise click.ClickException("Set DATABASE_URL to a local development database first.")
        if engine.dialect.name == "mysql" and engine.url.host not in {"localhost", "127.0.0.1", "::1"}:
            raise click.ClickException(
                "Refusing to create a table on a remote MySQL server. Apply the reviewed migration there separately."
            )
        if engine.dialect.name not in {"sqlite", "mysql"}:
            raise click.ClickException("This command supports local SQLite or localhost MySQL only.")
        Employee.__table__.create(engine, checkfirst=True)
        click.echo("Employee table is ready in the local database.")

    @app.cli.command("create-admin")
    @click.option("--employee-id", prompt="Initial administrator employee ID")
    def create_initial_admin(employee_id: str):
        """Create the first administrator after the employees table is migrated."""
        employee_id = employee_id.strip()
        if not _valid_employee_id(employee_id):
            raise click.ClickException(
                "Employee ID must be 3-64 characters and use letters, numbers, dot, underscore, or hyphen."
            )

        engine = app.extensions.get("database_engine")
        if engine is None:
            raise click.ClickException("Set DATABASE_URL before creating the administrator.")
        if not inspect(engine).has_table("employees"):
            raise click.ClickException(
                "The employees table is missing. Apply the additive employee-table migration first."
            )

        password = click.prompt("Administrator password", hide_input=True, confirmation_prompt=True)
        if not _valid_password(password):
            raise click.ClickException("Password must contain between 12 and 1024 characters.")
        password_hash = generate_password_hash(password)
        del password

        with Session(engine) as db_session, db_session.begin():
            if db_session.scalar(
                select(func.count(Employee.id)).where(Employee.role == "admin")
            ):
                raise click.ClickException(
                    "An administrator already exists; use an authenticated admin account to manage employees."
                )
            if db_session.scalar(
                select(Employee.id).where(Employee.employee_id == employee_id)
            ):
                raise click.ClickException("That employee ID is already in use.")
            db_session.add(
                Employee(
                    employee_id=employee_id,
                    password_hash=password_hash,
                    role="admin",
                    is_active=True,
                )
            )
        click.echo(f"Created initial administrator {employee_id}.")


@auth.route("/login", methods=["GET", "POST"])
def login():
    if not current_app.secret_key:
        return render_template(
            "service_error.html",
            active_page="service-error",
            message="Set SECRET_KEY in the environment and restart the application.",
        ), 503
    if getattr(g, "current_employee", None) is not None:
        return redirect(url_for("overview"))

    error = None
    if request.method == "POST":
        employee_id = request.form.get("employee_id", "").strip()
        password = request.form.get("password", "")
        engine = current_app.extensions.get("database_engine")
        if engine is None:
            error = "Employee authentication is temporarily unavailable."
        elif not employee_id or not password or len(password) > 1024:
            error = "Employee ID or password is incorrect."
        else:
            with Session(engine) as db_session:
                employee = db_session.scalar(
                    select(Employee).where(Employee.employee_id == employee_id)
                )
            if (
                employee is None
                or not employee.is_active
                or not check_password_hash(employee.password_hash, password)
            ):
                error = "Employee ID or password is incorrect."
            else:
                session.clear()
                session.permanent = True
                session["employee_pk"] = employee.id
                destination = _safe_next_url(request.form.get("next"))
                return redirect(destination or url_for("overview"))

    return render_template(
        "login.html",
        active_page="login",
        include_charts=False,
        error=error,
        next_url=_safe_next_url(request.values.get("next")),
    )


@auth.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("auth.login"))


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        employee = getattr(g, "current_employee", None)
        if employee is None or employee.role != "admin":
            abort(403)
        return view(*args, **kwargs)

    return wrapped


@auth.route("/admin/employees", methods=["GET", "POST"])
@admin_required
def manage_employees():
    engine = _employee_engine()
    if request.method == "POST":
        employee_id = request.form.get("employee_id", "").strip()
        password = request.form.get("password", "")
        role = request.form.get("role", "employee")
        if not _valid_employee_id(employee_id):
            flash("Use a 3-64 character employee ID with letters, numbers, dot, underscore, or hyphen.", "error")
        elif role not in {"admin", "employee"}:
            flash("Choose a valid employee role.", "error")
        elif not _valid_password(password):
            flash("Password must contain between 12 and 1024 characters.", "error")
        else:
            try:
                with Session(engine) as db_session, db_session.begin():
                    if db_session.scalar(
                        select(Employee.id).where(Employee.employee_id == employee_id)
                    ):
                        flash("That employee ID is already in use.", "error")
                    else:
                        db_session.add(
                            Employee(
                                employee_id=employee_id,
                                password_hash=generate_password_hash(password),
                                role=role,
                                is_active=True,
                            )
                        )
                        flash(f"Created {role} account {employee_id}.", "success")
            except IntegrityError:
                flash("That employee ID is already in use.", "error")
            finally:
                password = ""

    with Session(engine) as db_session:
        employees = db_session.scalars(
            select(Employee).order_by(Employee.employee_id)
        ).all()
    return render_template(
        "employees.html",
        active_page="employees",
        include_charts=False,
        employees=employees,
    )


@auth.post("/admin/employees/<int:employee_pk>/deactivate")
@admin_required
def deactivate_employee(employee_pk: int):
    engine = _employee_engine()
    with Session(engine) as db_session, db_session.begin():
        target = db_session.get(Employee, employee_pk)
        if target is None:
            abort(404)
        if target.id == g.current_employee.id:
            flash("You cannot deactivate your own administrator account.", "error")
        elif target.is_active and target.role == "admin":
            active_admin_count = db_session.scalar(
                select(func.count(Employee.id)).where(
                    Employee.role == "admin", Employee.is_active.is_(True)
                )
            ) or 0
            if active_admin_count <= 1:
                flash("The last active administrator cannot be deactivated.", "error")
            else:
                target.is_active = False
                flash(f"Deactivated employee {target.employee_id}.", "success")
        elif target.is_active:
            target.is_active = False
            flash(f"Deactivated employee {target.employee_id}.", "success")
    return redirect(url_for("auth.manage_employees"))
