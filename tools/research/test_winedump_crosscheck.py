from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from winedump_crosscheck import (  # noqa: E402
    CrosscheckError,
    compare_invariants,
    parse_winedump,
)


SYNTHETIC_DUMP = """\
Contents of /invented/NOT-A-VENDOR-FILE.EXE: 123 bytes

File header:
Entry point:         2:0042
Number of segments:  2
Number of modrefs:   3

Resources:
  STRING name 0001 flags 0030 length 0010
    00000000: 00 11 22 33                                      ....

Segment 1:
  File offset: 00000100
  Length:      00000020
  Flags:       00000150 (MOVEABLE PRELOAD RELOC_DATA)
  Alloc size:  00000030
  Relocations:
     1: sel = 1:0000
     2: ptr32 = SYNTHETIC.1

Segment 2:
  File offset: 00000200
  Length:      00000010
  Flags:       00000001 (DATA)
  Alloc size:  00000040
Done dumping /invented/NOT-A-VENDOR-FILE.EXE
"""


def test_parser_selects_only_stable_invented_invariants() -> None:
    parsed = parse_winedump(SYNTHETIC_DUMP)
    assert parsed == {
        "entry_point": {"cs": 2, "ip": 0x42},
        "segment_count": 2,
        "module_reference_count": 3,
        "segments": [
            {
                "index": 1,
                "file_offset": 0x100,
                "stored_length": 0x20,
                "flags_raw": 0x150,
                "allocation_size": 0x30,
                "relocation_record_count": 2,
            },
            {
                "index": 2,
                "file_offset": 0x200,
                "stored_length": 0x10,
                "flags_raw": 1,
                "allocation_size": 0x40,
                "relocation_record_count": 0,
            },
        ],
    }


@pytest.mark.parametrize(
    "changed, message",
    [
        (
            SYNTHETIC_DUMP.replace("  Flags:       00000001 (DATA)\n", ""),
            "lacks required fields",
        ),
        (
            SYNTHETIC_DUMP.replace("     2: ptr32", "     3: ptr32"),
            "incomplete relocation numbering",
        ),
        (
            SYNTHETIC_DUMP.replace("Segment 2:", "Segment 3:"),
            "one contiguous block",
        ),
        (
            SYNTHETIC_DUMP.replace("Number of segments:  2", "Number of segments:  0"),
            "outside the",
        ),
    ],
)
def test_parser_fails_closed_on_incomplete_synthetic_output(
    changed: str, message: str
) -> None:
    with pytest.raises(CrosscheckError, match=message):
        parse_winedump(changed)


def test_comparison_is_aggregate_on_match_and_precise_on_disagreement() -> None:
    expected = parse_winedump(SYNTHETIC_DUMP)
    observed = parse_winedump(SYNTHETIC_DUMP)
    matches, disagreements = compare_invariants(expected, observed)
    assert not disagreements
    assert {match["invariant"] for match in matches} == {
        "entry_point",
        "segment_count",
        "module_reference_count",
        "segment_file_offset",
        "segment_stored_length",
        "segment_flags_raw",
        "segment_allocation_size",
        "segment_relocation_record_count",
    }

    observed["segments"][1]["flags_raw"] ^= 1
    _, disagreements = compare_invariants(expected, observed)
    assert disagreements == [
        {
            "invariant": "segment_flags_raw",
            "details": [{"segment": 2, "expected": 1, "observed": 0}],
            "details_complete": True,
        }
    ]


def test_parser_does_not_return_banner_resource_or_name_text() -> None:
    encoded = repr(parse_winedump(SYNTHETIC_DUMP))
    assert "/invented" not in encoded
    assert "STRING" not in encoded
    assert "SYNTHETIC" not in encoded
