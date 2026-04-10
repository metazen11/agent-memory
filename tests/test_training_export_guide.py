"""Unit tests for shared training export guide."""

from app.training_export_guide import build_training_export_guide


def test_training_export_guide_has_api_and_mcp_surfaces():
    guide = build_training_export_guide()
    assert guide["api"]["help_endpoint"] == "/api/tool-calls/export/help"
    assert guide["api"]["dataset_endpoint"] == "/api/tool-calls/export/dataset"
    assert "training_export_guide" in guide["mcp"]["tools"]
    assert "export_training_dataset" in guide["mcp"]["tools"]


def test_training_export_guide_documents_dataset_types():
    guide = build_training_export_guide()
    shapes = guide["dataset_shapes"]
    assert "sft" in shapes
    assert "trajectory" in shapes
    assert "preference" in shapes

