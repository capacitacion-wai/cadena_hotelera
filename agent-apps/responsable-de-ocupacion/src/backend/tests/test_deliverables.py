"""Unit tests for app.deliverables (QA vs deliverable filtering)."""
from app.deliverables import (
    artifact_kind,
    filter_run_artifacts,
    is_qa_artifact,
    sanitize_attachment_list,
)


def test_is_qa_slide_jpg():
    assert is_qa_artifact("slide-01.jpg")
    assert is_qa_artifact("", "exports/slide-06.jpg")
    assert not is_qa_artifact("informe.pdf")


def test_filter_prefers_pptx_over_slides():
    raw = [
        {"filename": "slide-01.jpg", "workspace_path": "exports/slide-01.jpg", "size": 100},
        {"filename": "slide-02.jpg", "workspace_path": "exports/slide-02.jpg", "size": 100},
        {
            "filename": "situacion.pptx",
            "workspace_path": "exports/situacion.pptx",
            "size": 200000,
        },
        {
            "filename": "chart.png",
            "workspace_path": "exports/_assets_situacion/chart.png",
            "size": 50000,
        },
    ]
    out = filter_run_artifacts(raw)
    paths = [a["workspace_path"] for a in out]
    assert paths == ["exports/situacion.pptx"]
    assert out[0]["kind"] == "deliverable"


def test_filter_keeps_standalone_chart_when_no_primary():
    raw = [
        {"filename": "ventas.png", "workspace_path": "exports/ventas.png", "size": 1},
    ]
    out = filter_run_artifacts(raw)
    assert len(out) == 1
    assert out[0]["kind"] == "image"


def test_sanitize_attachment_list_strips_slides():
    raw = [
        {"filename": "slide-03.jpg", "workspace_path": "exports/slide-03.jpg"},
        {"filename": "report.pdf", "workspace_path": "exports/report.pdf"},
    ]
    out = sanitize_attachment_list(raw)
    assert len(out) == 1
    assert out[0]["filename"] == "report.pdf"


def test_artifact_kind():
    assert artifact_kind("deck.pptx") == "deliverable"
    assert artifact_kind("slide-01.jpg") == "qa"
