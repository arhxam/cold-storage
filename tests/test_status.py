from datetime import UTC, datetime, timedelta

from coldstorage.config import Config, ConnectorConfig
from coldstorage.engine import Engine
from coldstorage.status import compute_status, human_bytes, is_stale
from coldstorage.store.archive import Archive


def test_is_stale_never_run():
    assert is_stale(None) is True


def test_is_stale_recent_vs_old():
    now = datetime(2026, 1, 10, tzinfo=UTC)
    recent = (now - timedelta(days=2)).isoformat()
    old = (now - timedelta(days=9)).isoformat()
    assert is_stale(recent, now) is False
    assert is_stale(old, now) is True


def test_is_stale_handles_naive_timestamps():
    now = datetime(2026, 1, 10, tzinfo=UTC)
    naive_recent = datetime(2026, 1, 9).isoformat()  # no tz
    assert is_stale(naive_recent, now) is False


def test_compute_status_after_ingest(layout, instagram_export):
    with Archive(layout) as arc:
        Engine(arc).ingest(instagram_export)
        cfg = Config()
        st = compute_status(arc, cfg)
        assert st.total_records > 0
        ig = next(c for c in st.connectors if c.connector == "instagram")
        assert ig.last_status == "ok"
        assert ig.stale is False  # just ran
        assert st.any_stale is False


def test_compute_status_includes_configured_but_unrun(layout):
    with Archive(layout) as arc:
        cfg = Config(connectors={"telegram": ConnectorConfig()})
        st = compute_status(arc, cfg)
        tg = next(c for c in st.connectors if c.connector == "telegram")
        assert tg.records == 0
        assert tg.last_status is None
        assert tg.stale is True  # never run → stale (dead-man's-switch)
        assert st.any_stale is True


def test_human_bytes():
    assert human_bytes(0) == "0 B"
    assert human_bytes(1536).endswith("KB")
    assert human_bytes(5 * 1024 * 1024).endswith("MB")
