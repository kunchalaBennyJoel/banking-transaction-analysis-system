# Banking Transaction Analysis System

A local analytics and demonstration application for synthetic banking transactions. It does not connect to real bank accounts, move money, or establish that a flagged transaction is fraud.

## Current implementation

The Flask application provides a process health endpoint at `GET /health`; it does not contact MySQL. Employee login protects the dashboard and all `/api/` endpoints. Administrators manage employee access; passwords are stored as Werkzeug hashes. Read-only JSON endpoints expose transaction analytics and paginated transaction records. The database layer defines synthetic accounts and transactions, includes a deterministic seed command, and provides a reusable analytics service. Default seed data is synthetic, denominated in USD, and covers January 1, 2023 through October 18, 2026, inclusive, with a continuous daily baseline and additional variation in daily volume. Dashboard amounts are displayed in INR without converting those stored USD-valued amounts. It contains no customer or personal information.

### Requirements

- Python 3.10 or newer
- Git
- MySQL 8.0.16 or newer for enforcing the schema's `CHECK` constraints and running the seed command (database tests use SQLite and need no server)

Install the pinned project dependencies in a virtual environment:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

If the `py` launcher is unavailable, use an installed Python executable to create the virtual environment.

### Run the application

```powershell
python app.py
```

Open `http://127.0.0.1:5000/health`. A successful response is `{"status":"ok"}`.
Set `SECRET_KEY` and `DATABASE_URL`, create the local employee table and initial administrator as described below, then sign in at `http://127.0.0.1:5000/login`.

## Deploy on Render

Create a Render **Web Service** for this repository. Use these commands:

| Render setting | Value |
| --- | --- |
| Build Command | `pip install -r requirements.txt` |
| Start Command | `gunicorn --bind 0.0.0.0:$PORT wsgi:app` |
| Health Check Path (optional) | `/health` |

The `wsgi:app` target imports the Flask application object from `wsgi.py`. Gunicorn binds to all interfaces on Render's assigned `PORT`; do not use the local `python app.py` development command for the hosted service. Render's Flask guidance uses `pip install -r requirements.txt` for dependency installation and Gunicorn as the production server ([Render Flask deployment guide](https://render.com/docs/deploy-flask), [Render port binding](https://render.com/docs/web-services#port-binding)).

In the Render service's Environment settings, configure `APP_ENV=production`, `APP_DEBUG=0`, `SECRET_KEY`, and `DATABASE_URL`. Set `DATABASE_URL` to the connection URL for the existing MySQL database, using the `mysql+pymysql://` SQLAlchemy scheme. Store secrets only in Render's environment settings; do not commit them. Before deploying authenticated code, apply `migrations/001_add_employees.sql` and `migrations/002_add_transaction_channel.sql` once to the intended database, then create the initial administrator with the CLI below. These additive migrations create the employee table and nullable transaction channel column; existing transactions remain unchanged and their channel remains NULL until an explicit backfill. Startup does not run migrations, create tables, seed data, or reset records. The health endpoint reports process availability without checking database connectivity.

Chart.js is loaded from jsDelivr, so the browser needs access to that CDN for charts. This repository change prepares the application configuration and start command; it does not create a Render service or deploy the app.

Settings are read from the process environment. `.env.example` documents the supported names; it is a template and is not loaded automatically. No dotenv package is used. Set values in PowerShell, for example:

```powershell
$env:APP_ENV = "development"
$env:APP_DEBUG = "0"
$env:SECRET_KEY = .\.venv\Scripts\python.exe -c "import secrets; print(secrets.token_urlsafe(48))"
$env:DATABASE_URL = "mysql+pymysql://bank_demo:replace_me@127.0.0.1:3306/bank_analysis"
```

Do not display, commit, or share the generated key or a real database URL. Flask does not load `.env` files automatically; set these variables in the same shell before starting the app.

### Employee authentication and local setup

There is no public registration. Create the initial admin through a hidden password prompt; the command stores only a Werkzeug password hash. The local table command refuses remote MySQL hosts and creates only the `employees` table:

```powershell
python -m flask --app app:create_app init-employee-table
python -m flask --app app:create_app create-admin
python app.py
```

`create-admin` requires the employee table to exist and refuses to create a second initial admin. Administrators can add employees and deactivate accounts at `/admin/employees`. Passwords must be at least 12 characters; they are never shown in the UI or stored in plaintext. In production, use HTTPS so secure session cookies are sent only over TLS.

For an existing production MySQL database, review and apply `migrations/001_add_employees.sql` and `migrations/002_add_transaction_channel.sql` as one-time additive migrations, then run `create-admin` from a controlled environment with the intended `DATABASE_URL`. `migrations/003_backfill_transaction_channels.sql` is a separate, explicit data update for the known 2,400 NULL-channel synthetic rows; review its expected-state checks and results before deciding to run it. Do not run the local-only `init-employee-table` command against Aiven; the CLI rejects remote MySQL hosts. This project does not use Alembic, so track the SQL migrations as applied in your deployment procedure. No production migration, channel backfill, or account creation has been run here.

For development, create or inspect the MySQL database first and run `python -m app.seed` to create missing tables and add the deterministic synthetic sample. The default seed requests 2,400 events: it retains the original deterministic 2025–2026 records through October 18, 2026 and adds 1,200 events across 2023–2024, with one event for each day in that historical period and additional repeat events. On a fresh database, original-sequence records dated after October 18, 2026 are omitted, so its total is slightly below 2,400. Existing rows, including any later-dated rows created by an older seed version, are never updated or deleted. Rerunning the command skips references already present and adds only missing synthetic accounts or transactions. Review the target database before running a development seed so synthetic records are not mixed into a production dataset. Replace the example connection values with your local MySQL username and password; set real credentials in your shell environment and never commit them. `.env` files are ignored by Git, but this project does not load them automatically. `create_all` creates missing tables but does not migrate existing schemas.

The schema stores each transaction as one event. `amount` is a positive `DECIMAL(12, 2)` value and is not a net balance or profit. Deposits have a destination account, withdrawals have a source account, and transfers/payments have both. A transfer is not duplicated into two transaction rows. Timestamp values are stored in UTC. The sample data uses USD only; future analytics must group by currency before summing.

## Transaction analytics

`app.analytics.summarize_transactions(session, start_at=..., end_at=...)` reads actual transaction rows through SQLAlchemy and returns a typed summary. It is independent of Flask routes and does not create or manage the database engine.

- Total transaction count, status counts, type counts, and daily volume include every transaction event in the selected window, including pending and failed events.
- Successful transaction count and value include only rows with status `successful`. The average uses those same rows, is rounded to two decimal places with decimal half-up rounding, and is `None` when there are no successful rows.
- Deposit and withdrawal totals include only successful deposits or withdrawals, respectively. They report event value, not a signed net movement, balance, or profit.
- Each transfer is already one row, so it contributes once to event counts and once to successful value when its status is `successful`.
- Monetary calculations use `Decimal`. The current analytics service supports USD only and raises a clear error if the selected window contains another currency; it never combines currencies.
- Supplied date boundaries must be timezone-aware. They are normalized to UTC; persisted `occurred_at` values are interpreted as UTC. The range is half-open: `start_at` is included and `end_at` is excluded. Either bound may be omitted; if both are provided, the daily series includes every UTC day touched by the range, with zero counts for days without records.

The analytics tests use isolated in-memory SQLite databases, so no MySQL server is needed:

```powershell
pytest tests/test_analytics.py -q
pytest -q
```

## Read-only API

Configure `DATABASE_URL` and `SECRET_KEY` in the process environment. The app factory creates an engine without opening a connection; `/health` remains public and independent of database configuration and availability. Every `/api/` endpoint requires an active employee session and returns JSON 401 when unauthenticated. The API does not create tables or modify transaction records.

- `GET /api/analytics/summary` returns transaction counts, counts by status and type, successful USD value metrics, and daily volume for records before the exclusive UTC cutoff `2026-10-19 00:00:00` (through October 18, 2026). Pending and failed events count toward event totals; monetary totals include successful events only. Exact monetary values are JSON strings with two decimal places. The average is `null` if there are no successful rows. Analytics currently supports USD only.
- `GET /api/analytics/volume?start_date=YYYY-MM-DD&end_date=YYYY-MM-DD` returns daily event counts and all supported summary metrics for the selected range. Both dates are required; the start is inclusive and the end is exclusive. Boundaries and returned days use UTC, and empty days have a zero count. Successful value/deposit/withdrawal amounts only include successful transactions.
- `GET /api/transactions` returns a paginated list, ordered by occurrence time descending and then ID descending. Optional filters are `status` (`successful`, `pending`, or `failed`), `transaction_type` (`deposit`, `withdrawal`, `transfer`, or `payment`), and paired `start_date`/`end_date` values using the same UTC inclusive/exclusive rule. `page` defaults to `1`; `page_size` defaults to `25` and cannot exceed `100`.
- `GET /api/accounts/transactions?account_number=SYN-ACCT-0001` returns the matching account's paginated transactions, masked account identifier, and count/debit/credit totals for the active filters. Optional status, type, page, and date parameters match `/api/transactions`. Debit and credit totals include successful outgoing and incoming events respectively; they are event values, not balances. Search references are synthetic only.

Successful responses use a `data` field; transaction-list responses also include `pagination` metadata. API errors use JSON shaped as `{"error":{"code":"...","message":"..."}}`. Unauthenticated requests return HTTP 401. Invalid and unknown query parameters return HTTP 400 after authentication. Missing routes and unsupported methods return JSON 404 and 405 responses. Database failures return a generic 503 response without exposing connection details.

Examples:

```text
GET /api/analytics/summary
GET /api/analytics/volume?start_date=2025-01-01&end_date=2025-02-01
GET /api/transactions?page=1&page_size=25&status=successful&transaction_type=transfer
GET /api/accounts/transactions?account_number=SYN-ACCT-0001&page=1&page_size=25
```

## Dashboard

The application has three main Flask pages with shared top navigation: `/` (Overview), `/transactions` (Transactions), and `/analysis` (Analysis). All require an active employee login. Admins also have `/admin/employees` for account management. The pages use Jinja templates, standalone CSS, and vanilla JavaScript under `app/templates/` and `app/static/`.

- **Overview** displays the summary, daily volume, status distribution, and type distribution for transactions before the exclusive UTC cutoff `2026-10-19 00:00:00` (through October 18, 2026). Event counts include all statuses.
- **Transactions** is dedicated to the paginated read-only table with account search plus status, type, channel, and date filters. Account search uses the existing synthetic account references, shows a masked reference and totals for the current filters, and includes events where the account is the source or destination. Successful outgoing/incoming event amounts are labeled as debits/credits; they are not a balance. Visible dates include both the start and through date. Requests translate the through date to the following UTC midnight, matching the API's exclusive end boundary. CSV, PDF, and Word exports include all matching rows (up to 10,000), independent of the current page. Clear search returns to the normal transaction listing.
- **Analysis** provides a date selector, daily volume, status/type counts, channel counts for UPI, ATM, Branch, and Not recorded, and successful transaction, deposit, and withdrawal amounts from the selected period. Channel-specific CSV links retain the selected date range. Dashboard amounts use INR formatting without converting the stored USD-valued amounts; date-filtered figures all refresh together from the existing analytics service via `/api/analytics/volume`.

Successful totals include successful events only; transaction amounts are not balances or profit, and analytics do not establish fraud. Transfers remain one event. Analytics is USD-only. The demonstration dataset is synthetic and is not connected to real bank accounts. Date filters initially cover January 2025, which remains populated in the expanded seed; reset restores that range.

Chart.js 4.5.1 is loaded from the jsDelivr CDN by the dashboard template. The dashboard does not require a JavaScript runtime, package manager, or frontend build step. Internet access is needed to load the chart library; if the CDN is unavailable, the other dashboard sections remain usable and the charts show a clear message.

## Dependencies

- Flask — HTTP application, JSON routes, and the database-independent health route.
- Gunicorn — production WSGI server used by Render to bind to its assigned host port.
- SQLAlchemy — ORM mappings, schema constraints, and parameterized database access.
- Flask-WTF — CSRF protection for login and employee-management forms.
- Werkzeug — password hashing and verification used for employee credentials.
- ReportLab — paginated PDF transaction reports.
- python-docx — formatted Word transaction reports.
- PyMySQL — Python MySQL driver used by the `mysql+pymysql` SQLAlchemy URL.
- pytest — automated checks for health, authentication, employee management, database schema and seed behavior, analytics, and API responses.
- Chart.js — browser-side charts loaded from a pinned CDN URL; it is not a Python package or build dependency.

Flask's supporting packages and pytest's supporting packages are installed transitively by pip. The current backend and dashboard do not require Pandas or NumPy.

### Run checks

```powershell
.\.venv\Scripts\python.exe -m pytest
```

## File purpose inventory

- `app.py` — starts the local Flask development server.
- `wsgi.py` — exports the Flask application object used by the Gunicorn production command.
- `app/__init__.py` — creates the Flask application, configures routes, and serves the dashboard and database-independent health endpoint.
- `app/auth.py` — handles employee login/logout, CSRF checks, route protection, employee administration, and first-admin CLI setup.
- `app/exporters.py` — renders bounded CSV, PDF, and Word exports; CSV text cells are protected against spreadsheet formula injection.
- `app/templates/base.html` — shared page shell, accessible top navigation, and frontend asset references.
- `app/templates/login.html` — employee sign-in form.
- `app/templates/employees.html` — admin-only employee creation and deactivation page.
- `app/templates/service_error.html` — safe message for authentication and database service errors.
- `app/templates/overview.html` — summary and activity visualizations through October 18, 2026 (UTC).
- `app/templates/transactions.html` — transaction filters, table, pagination controls, and export actions.
- `app/templates/analysis.html` — date selector, selected-period metrics, and analysis charts.
- `app/static/css/dashboard.css` — responsive visual styling, table and status treatments, and keyboard-focus states.
- `app/static/js/api.js` — read-only fetch helpers, API error mapping, and date/pagination query construction.
- `app/static/js/dashboard.js` — loads API data, updates metrics/charts/table, validates filters, and handles pagination.
- `app/config.py` — reads environment settings, including database and session secrets, and configures secure session cookies.
- `app/api.py` — defines read-only analytics, transaction-list, and account-transaction search routes, input validation, JSON serialization, and JSON error responses.
- `app/database.py` — declares the SQLAlchemy base, creates engines, and creates missing model tables; imports do not open a database connection.
- `app/analytics.py` — calculates date-filtered counts by status, type, and channel, USD successful-value metrics, and daily event counts from a SQLAlchemy session.
- `app/models.py` — defines employee, synthetic account, and transaction fields, relationships, indexes, and database constraints.
- `migrations/001_add_employees.sql` — additive MySQL migration that creates only the employee authentication table.
- `migrations/002_add_transaction_channel.sql` — additive MySQL migration that adds nullable transaction channel metadata; existing records display as Not recorded until explicitly assigned.
- `migrations/003_backfill_transaction_channels.sql` — guarded, repeatable update for the requested synthetic channel allocation; it is never run automatically.
- `app/seed.py` — generates deterministic, PII-free sample data and exposes `python -m app.seed`.
- `tests/test_health.py` — checks the health endpoint response using Flask's in-process test client.
- `tests/test_auth.py` — checks login, CSRF, protected routes, admin operations, password hashing, session revocation, and CLI setup using SQLite.
- `tests/test_analytics.py` — verifies analytics status/type handling, Decimal math, UTC date boundaries, empty ranges, and USD-only behavior with SQLite.
- `tests/test_api.py` — checks JSON analytics, date filters, pagination, validation, errors, and database-independent health behavior using SQLite.
- `tests/test_dashboard.py` — checks the three page routes, active navigation, distinct page content, and local asset serving without a database.
- `tests/test_database.py` — verifies seed behavior and schema constraints with an in-memory SQLite database.
- `requirements.txt` — pins Flask, Gunicorn, SQLAlchemy, database drivers, export libraries, and pytest.
- `.env.example` — documents safe optional environment settings; it contains no secrets.
- `.gitignore` — excludes environment files, virtual environments, caches, and editor or OS metadata.
- `README.md` — explains scope, setup, startup, tests, and file responsibilities.

## Git and GitHub

`git init` creates a local repository. `git add` selects files for the next snapshot, and `git commit` records that snapshot locally. A GitHub repository is a separate remote copy: `git remote add origin <URL>` connects it, and `git push -u origin main` uploads the local commit and sets the upstream branch. Do not push until the GitHub repository has been created and the remote URL has been verified.
