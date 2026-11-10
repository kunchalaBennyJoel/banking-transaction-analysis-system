def test_production_wsgi_entry_point_serves_health_without_database():
    from wsgi import app

    response = app.test_client().get("/health")

    assert response.status_code == 200
    assert response.get_json() == {"status": "ok"}
