from app import db


def test_meta_round_trip(conn):
    assert db.get_meta(conn, "garmin_synced_at") is None
    db.set_meta(conn, "garmin_synced_at", "2026-01-01T00:00:00+00:00")
    db.set_meta(conn, "garmin_synced_at", "2026-01-02T00:00:00+00:00")
    assert db.get_meta(conn, "garmin_synced_at") == "2026-01-02T00:00:00+00:00"
