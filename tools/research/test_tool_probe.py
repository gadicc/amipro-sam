from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from tool_probe import (  # noqa: E402
    MAX_VERSION_CHARS,
    SCHEMA,
    _bounded_text,
    build_tool_report,
)


def test_version_text_is_single_line_and_bounded() -> None:
    value = _bounded_text(b"noise\nTool version 1.2.3\n", b"")
    assert value == "Tool version 1.2.3"
    assert len(value) <= MAX_VERSION_CHARS


def test_empty_version_output_is_unknown() -> None:
    assert _bounded_text(b"", b"") is None


def test_tool_report_has_one_canonical_shape() -> None:
    modules = [
        {
            "name": "synthetic-module",
            "available": False,
            "version": None,
            "research_context": "invented test record",
        }
    ]
    report = build_tool_report([], modules)
    assert report["schema"] == SCHEMA
    assert report["python"]["implementation"]
    assert report["probes"] == []
    assert report["python_modules"] == modules
    assert "tools" not in report
