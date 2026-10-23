from datetime import UTC, datetime, timedelta

from app.core.config import Settings
from app.schemas.common import GeoPoint, Source
from app.schemas.hospital import Hospital, Resource
from app.schemas.incident import Incident, LocationCandidate
from app.services.duplicate import suggest_duplicates, text_similarity
from app.services.hospital_service import make_view

NOW = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)


def hosp(age_s, status="available"):
    t = None if age_s is None else NOW - timedelta(seconds=age_s)
    return Hospital(id="H", name="H", location=GeoPoint(lat=30.9, lon=75.85), capabilities=["cardiac"],
                    resources={"cath_lab": Resource(status=status, count=2, last_confirmed_at=t,
                                                    source=Source.reported)})


def test_fresh_available_stays_available():
    assert make_view(hosp(60), NOW, 1800).resources["cath_lab"].status == "available"


def test_stale_available_becomes_unknown():
    r = make_view(hosp(7200), NOW, 1800).resources["cath_lab"]
    assert r.status == "unknown" and r.reported_status == "available" and r.stale


def test_never_confirmed_is_unknown():
    assert make_view(hosp(None), NOW, 1800).resources["cath_lab"].status == "unknown"


def test_stale_unavailable_stays_unavailable():
    assert make_view(hosp(7200, "unavailable"), NOW, 1800).resources["cath_lab"].status == "unavailable"


def inc(id_, minutes_ago, lat, lon, status="new"):
    return Incident(id=id_, created_at=NOW - timedelta(minutes=minutes_ago), status=status,
                    location_candidates=[LocationCandidate(lat=lat, lon=lon, accuracy_m=10)])


def test_text_similarity_bounds():
    assert text_similarity("man collapsed outside bus stand", "man collapsed near the bus stand") > 0.5
    assert text_similarity("fire in kitchen", "road accident highway") == 0


def test_nearby_similar_recent_is_suggested():
    s = Settings()
    others = [(inc("INC-1", 3, 30.90010, 75.85010), "man collapsed outside bus stand")]
    out = suggest_duplicates(created_at=NOW, location=GeoPoint(lat=30.9002, lon=75.8502, accuracy_m=10),
                             text="man collapsed near bus stand", others=others, settings=s)
    assert [d.incident_id for d in out] == ["INC-1"] and out[0].status == "suggested"


def test_far_old_or_closed_not_suggested():
    s = Settings()
    here = GeoPoint(lat=30.9, lon=75.85, accuracy_m=10)
    txt = "man collapsed outside bus stand"
    far = [(inc("INC-2", 3, 30.95, 75.9), txt)]
    old = [(inc("INC-3", 60, 30.9, 75.85), txt)]
    closed = [(inc("INC-4", 3, 30.9, 75.85, status="closed"), txt)]
    for others in (far, old, closed):
        assert suggest_duplicates(created_at=NOW, location=here, text=txt, others=others, settings=s) == []
