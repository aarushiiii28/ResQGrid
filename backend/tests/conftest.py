import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.core.security import create_token, find_user
from app.main import create_app


def token_for(user_id: str | None = None, role: str | None = None) -> dict:
    return {"Authorization": f"Bearer {create_token(find_user(user_id, role))}"}


def make_client(**overrides):
    overrides.setdefault("intake_wait_s", 0.0)  # tests dispatch at once; the intake wait has its own tests
    s = get_settings().model_copy(update=overrides)
    app = create_app(s, database_url=s.test_database_url, background=False)
    return TestClient(app)


@pytest.fixture
def client():
    """Deterministic API client: simulated crew and volunteers are OFF, so tests drive every step."""
    with make_client(sim_driver=False, sim_volunteers=False, demo_autopilot_delay_s=0.0) as c:
        r = c.post("/api/admin/reset-demo", headers=token_for(role="admin"))
        assert r.status_code == 200, r.text
        yield c


@pytest.fixture
def sim_client():
    """Whole flow runs hands-free and fast: simulated crew, volunteers, Demo Autopilot."""
    with make_client(sim_driver=True, sim_volunteers=True, demo_autopilot=True, demo_autopilot_delay_s=0.0,
                     sim_driver_delay_s=0.02, sim_on_scene_hold_s=0.02, sim_speed_factor=3000.0,
                     sim_tick_s=0.01) as c:
        r = c.post("/api/admin/reset-demo", headers=token_for(role="admin"))
        assert r.status_code == 200, r.text
        yield c


@pytest.fixture
def ctx(client):
    return client.app.state.ctx


@pytest.fixture
def run(client):
    """Run a coroutine function on the app's event loop."""

    def _run(fn, *args, **kw):
        return client.portal.call(lambda: fn(*args, **kw))

    return _run


@pytest.fixture
def disp():
    return token_for(role="dispatcher")


@pytest.fixture
def admin():
    return token_for(role="admin")


@pytest.fixture
def caller():
    return token_for("caller-1")
