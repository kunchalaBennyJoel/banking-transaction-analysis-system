from app import create_app


def test_health_endpoint_reports_process_status_without_database():
    app = create_app({"TESTING": True})
    client = app.test_client()

    response = client.get("/health")

    assert response.status_code == 200
    assert response.get_json() == {"status": "ok"}
