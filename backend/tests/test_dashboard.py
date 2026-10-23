def test_dashboard_is_served_at_root(client):
    r = client.get("/")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "<title>Sensorstation</title>" in r.text


def test_static_assets_are_served(client):
    assert client.get("/app.js").headers["content-type"].startswith(("text/javascript", "application/javascript"))
    assert client.get("/style.css").headers["content-type"].startswith("text/css")


def test_chart_js_version_is_pinned(client):
    html = client.get("/").text
    assert "chart.js@4.5.1/" in html
    assert 'integrity="sha384-' in html


def test_api_routes_still_take_precedence(client):
    assert client.get("/api/health").json()["database"] is True
    assert client.get("/docs").status_code == 200


def test_unknown_path_returns_404(client):
    assert client.get("/gibtsnicht").status_code == 404
