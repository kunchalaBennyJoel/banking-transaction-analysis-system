import secrets

import pytest
from sqlalchemy.orm import Session
from werkzeug.security import generate_password_hash

from app import create_app
from app.database import create_schema
from app.models import Employee


@pytest.fixture
def client():
    app = create_app(
        {
            "TESTING": True,
            "DATABASE_URL": "sqlite+pysqlite:///:memory:",
            "SECRET_KEY": secrets.token_hex(32),
        }
    )
    create_schema(app.extensions["database_engine"])
    with Session(app.extensions["database_engine"]) as session, session.begin():
        employee = Employee(
            employee_id="DASHBOARD-ADMIN",
            password_hash=generate_password_hash("test-password-never-used"),
            role="admin",
        )
        session.add(employee)
        session.flush()
        employee_pk = employee.id
    test_client = app.test_client()
    with test_client.session_transaction() as flask_session:
        flask_session["employee_pk"] = employee_pk
        flask_session.permanent = True
    yield test_client
    app.extensions["database_engine"].dispose()


@pytest.mark.parametrize(
    ("path", "page"),
    [("/", "overview"), ("/transactions", "transactions"), ("/analysis", "analysis")],
)
def test_three_pages_serve_shared_navigation_with_correct_active_page(client, path, page):
    response = client.get(path)

    assert response.status_code == 200
    assert response.mimetype == "text/html"
    html = response.get_data(as_text=True)
    assert f'<body data-page="{page}">' in html
    assert 'href="/"' in html
    assert 'href="/transactions"' in html
    assert 'href="/analysis"' in html
    assert f'href="{path}" aria-current="page"' in html
    assert "Synthetic demonstration data" in html
    assert 'href="/static/css/dashboard.css"' in html
    assert 'src="/static/js/api.js"' in html
    assert 'src="/static/js/dashboard.js"' in html
    chart_script = 'src="https://cdn.jsdelivr.net/npm/chart.js@4.5.1/dist/chart.umd.min.js"'
    assert (chart_script in html) is (page != "transactions")


def test_pages_keep_distinct_purposes(client):
    overview = client.get("/").get_data(as_text=True)
    transactions = client.get("/transactions").get_data(as_text=True)
    analysis = client.get("/analysis").get_data(as_text=True)

    assert 'id="metric-total"' in overview
    assert 'id="overview-volume-chart"' in overview
    assert 'id="transactions-body"' not in overview
    assert 'id="transactions-body"' in transactions
    assert 'id="filter-status"' in transactions
    assert 'id="account-search-form"' in transactions
    assert 'id="account-number"' in transactions
    assert 'id="clear-account-search"' in transactions
    assert 'id="account-summary"' in transactions
    assert 'id="filter-channel"' in transactions
    assert "Not recorded" in transactions
    assert transactions.count('class="button button-quiet transaction-export"') == 3
    assert "Transactions by channel" in analysis
    assert 'data-channel="UPI"' in analysis
    assert 'data-channel="UNKNOWN"' in analysis
    assert "<dt>Not recorded</dt>" in analysis
    assert analysis.count("class=\"button button-quiet channel-export\"") == 4
    assert 'id="analysis-start"' not in transactions
    assert 'id="analysis-start"' in analysis
    assert 'id="analysis-deposits"' in analysis
    assert 'id="analysis-withdrawals"' in analysis
    assert "Through date (UTC, inclusive)" in analysis


def test_dashboard_date_controls_and_overview_describe_cutoff(client):
    overview = client.get("/").get_data(as_text=True)
    transactions = client.get("/transactions").get_data(as_text=True)
    analysis = client.get("/analysis").get_data(as_text=True)

    assert "THROUGH OCTOBER 18, 2026 · UTC" in overview
    assert "ALL-TIME" not in overview
    assert transactions.count('max="2026-10-18"') == 2
    assert analysis.count('max="2026-10-18"') == 2


def test_frontend_assets_are_served_by_flask(client):
    css = client.get("/static/css/dashboard.css")
    api_js = client.get("/static/js/api.js")
    dashboard_js = client.get("/static/js/dashboard.js")

    assert css.status_code == 200
    assert "text/css" in css.mimetype
    assert "prefers-reduced-motion" in css.get_data(as_text=True)
    assert api_js.status_code == 200
    assert api_js.mimetype in {"application/javascript", "text/javascript"}
    api_source = api_js.get_data(as_text=True)
    assert "getAnalysis" in api_source
    assert "getAccountTransactions" in api_source
    assert "/api/accounts/transactions?" in api_source
    assert dashboard_js.status_code == 200
    assert dashboard_js.mimetype in {"application/javascript", "text/javascript"}
    dashboard_source = dashboard_js.get_data(as_text=True)
    assert "aria-busy" in dashboard_source
    assert 'new Intl.NumberFormat("en-IN", {' in dashboard_source
    assert 'currency: "INR"' in dashboard_source
    assert "formatMoney(row.amount)" in dashboard_source
    assert '$("filter-channel").value' in dashboard_source
    assert "counts_by_channel" in dashboard_source
    assert "No transaction events are available for this chart." in dashboard_js.get_data(as_text=True)
