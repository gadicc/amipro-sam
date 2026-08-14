"""Apply the deterministic identity used by the bundled CJK fallback font."""

from __future__ import annotations

import argparse
from pathlib import Path

from fontTools.ttLib import TTFont

_FIXED_CREATED = 3702527940
_FIXED_MODIFIED = 3702528280
_NAME_VALUES = {
    1: "AmiPro Preservation CJK",
    2: "Regular",
    3: "AmiPro Preservation CJK 2.004",
    4: "AmiPro Preservation CJK Regular",
    6: "AmiProPreservationCJK-Regular",
    16: "AmiPro Preservation CJK",
    17: "Regular",
}
_REPLACED_NAME_IDS = (1, 2, 3, 4, 6, 16, 17, 18, 21, 22, 25)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="pre-renaming TrueType font")
    parser.add_argument("output", type=Path, help="derived TrueType font")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    font = TTFont(args.input, recalcTimestamp=False)
    name_table = font["name"]

    for name_id in _REPLACED_NAME_IDS:
        name_table.removeNames(nameID=name_id)
    for name_id, value in _NAME_VALUES.items():
        name_table.setName(value, name_id, 3, 1, 0x409)

    font["head"].created = _FIXED_CREATED
    font["head"].modified = _FIXED_MODIFIED
    font.save(args.output, reorderTables=True)


if __name__ == "__main__":
    main()
