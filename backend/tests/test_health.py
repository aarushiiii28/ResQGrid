def test_health_ok(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["database"] == "ok"
    assert body["mode"] == "DEMO / SIMULATED"


def test_public_summary_counts_only(client):
    r = client.get("/api/public/summary")
    assert r.status_code == 200
    s = r.json()
    assert s["units_total"] == 20 and s["hospitals_total"] == 12 and s["open_incidents"] == 0
    assert s["units_available"] <= 19  # AMB-20 is unavailable in the seed
    assert s["hospitals_fresh"] <= 12 and s["mode"] == "DEMO / SIMULATED"
    assert set(s) == {"units_total", "units_available", "units_unknown", "hospitals_total",
                      "hospitals_fresh", "open_incidents", "mode"}  # no identities, no locations
