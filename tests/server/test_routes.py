"""Route tests for the SyringeLiquidHandler /v1 API against FakeCell."""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_health_before_diagnose(client: TestClient) -> None:
    r = client.get("/v1/health")
    assert r.status_code == 200
    body = r.json()
    assert body["cell_up"] is True
    # No diagnose yet → per-device ok is unknown (null).
    assert body["pump_ok"] is None
    assert body["balance_ok"] is None


def test_diagnose_then_health(client: TestClient) -> None:
    d = client.get("/v1/diagnose")
    assert d.status_code == 200
    assert d.json()["ok_to_initialize"] is True
    h = client.get("/v1/health").json()
    assert h["pump_ok"] is True
    assert h["balance_ok"] is True
    assert h["driver_versions"]["pump"] == "FAKE-8.33"


def test_status_shape(client: TestClient) -> None:
    s = client.get("/v1/status").json()
    assert set(s) == {
        "weight_g",
        "valve",
        "plunger_uL",
        "stage_x_mm",
        "stage_z_mm",
        "busy",
        "error",
    }


def test_tare(client: TestClient) -> None:
    r = client.post("/v1/balance/tare")
    assert r.status_code == 200
    assert r.json()["weight_g"] == 0.0


def test_ambient_valid_and_invalid(client: TestClient) -> None:
    ok = client.post("/v1/balance/ambient", json={"level": "stable"})
    assert ok.status_code == 200 and ok.json()["level"] == "stable"
    bad = client.post("/v1/balance/ambient", json={"level": "nonsense"})
    assert bad.status_code == 400
    assert bad.json()["error"] == "InvalidArgError"


def test_pump_requires_init(client: TestClient) -> None:
    # Aspirate before initialize → 409 wrong-state.
    r = client.post("/v1/pump/aspirate", json={"target_uL": 50})
    assert r.status_code == 409
    assert r.json()["error"] == "WrongStateError"


def test_initialize_then_aspirate_dispense(client: TestClient) -> None:
    init = client.post("/v1/pump/initialize", json={"force": 2})
    assert init.status_code == 200
    assert init.json() == {"valve": "1", "plunger_uL": 0.0}

    asp = client.post("/v1/pump/aspirate", json={"target_uL": 80})
    assert asp.status_code == 200 and asp.json()["plunger_uL"] == 80.0

    disp = client.post("/v1/pump/dispense", json={"target_uL": 0})
    assert disp.status_code == 200 and disp.json()["plunger_uL"] == 0.0


def test_aspirate_overflow_is_400(client: TestClient) -> None:
    client.post("/v1/pump/initialize", json={"force": 2})
    r = client.post("/v1/pump/aspirate", json={"target_uL": 999})
    assert r.status_code == 400


def test_valve_and_cycle(client: TestClient) -> None:
    client.post("/v1/pump/initialize", json={"force": 2})
    v = client.post("/v1/pump/valve", json={"port": 3})
    assert v.status_code == 200 and v.json()["valve"] == "3"
    c = client.post(
        "/v1/pump/cycle",
        json={"cycles": 3, "volume_uL": 125, "source_port": 1, "dispense_port": 3},
    )
    assert c.status_code == 200
    assert c.json() == {"cycles_done": 3, "final_valve": "3"}


def test_stage_home_and_move(client: TestClient) -> None:
    h = client.post("/v1/stage/home")
    assert h.status_code == 200 and h.json() == {"x_mm": 0.0, "z_mm": 0.0}
    m = client.post(
        "/v1/stage/move",
        json={"x_mm": 261.5, "z_mm": 234.0, "speed_pct": 20, "accel_pct": 10},
    )
    assert m.status_code == 200
    assert m.json() == {"x_mm": 261.5, "z_mm": 234.0}


def test_stop_always_ok(client: TestClient) -> None:
    r = client.post("/v1/stop")
    assert r.status_code == 200 and r.json()["stopped"] is True


def test_validation_error_on_bad_port(client: TestClient) -> None:
    client.post("/v1/pump/initialize", json={"force": 2})
    # port out of schema range (ge=1, le=4) → 422 from pydantic.
    r = client.post("/v1/pump/valve", json={"port": 9})
    assert r.status_code == 422
