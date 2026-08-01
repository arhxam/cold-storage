from coldstorage.engine import Engine
from coldstorage.store.archive import Archive
from coldstorage.viewer import build_viewer


def test_build_viewer_is_self_contained(layout, instagram_export):
    with Archive(layout) as arc:
        Engine(arc).ingest(instagram_export)
        out = build_viewer(arc, layout.home / "viewer.html")
    assert out.exists()
    doc = out.read_text(encoding="utf-8")
    # self-contained: no external network resources
    assert "http://" not in doc and "https://" not in doc
    # data is embedded and the mojibake-repaired text made it in
    assert "café tomorrow?" in doc
    assert "instagram" in doc
    assert "<script>" in doc


def test_viewer_neutralizes_script_injection(layout):
    from coldstorage.models import Batch, NormalizedRecord, RecordType

    with Archive(layout) as arc:
        arc.ingest_batch(
            Batch(records=[NormalizedRecord(
                connector="x", type=RecordType.MESSAGE, uid="m1",
                text="<script>alert('xss')</script>",
            )])
        )
        out = build_viewer(arc, layout.home / "v.html")
    doc = out.read_text(encoding="utf-8")
    # the injected markup must be escaped, never present as live tags
    assert "<script>alert('xss')" not in doc
    assert "</script>alert" not in doc
    assert "\\u003cscript\\u003e" in doc  # escaped form is present instead
