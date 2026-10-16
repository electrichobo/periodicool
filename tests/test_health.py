from periodicool import __version__


def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "version": __version__}


def test_startup_creates_database(client, settings):
    assert settings.db_path.is_file()
