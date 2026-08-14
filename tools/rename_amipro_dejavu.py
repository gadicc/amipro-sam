"""Apply collision-resistant package identities to bundled DejaVu Sans faces."""

from __future__ import annotations

import argparse
from pathlib import Path

from fontTools.ttLib import TTFont

_STYLES = {
    "regular": ("Regular", "AmiProPreservationSans-Regular"),
    "bold": ("Bold", "AmiProPreservationSans-Bold"),
    "oblique": ("Oblique", "AmiProPreservationSans-Oblique"),
    "bold-oblique": ("Bold Oblique", "AmiProPreservationSans-BoldOblique"),
}
_REPLACED_NAME_IDS = (1, 2, 3, 4, 6, 16, 17, 18, 21, 22, 25)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("style", choices=sorted(_STYLES))
    parser.add_argument("input", type=Path, help="unmodified DejaVu 2.37 face")
    parser.add_argument("output", type=Path, help="renamed TrueType face")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    style, postscript = _STYLES[args.style]
    family = "AmiPro Preservation Sans"
    font = TTFont(args.input, recalcTimestamp=False)
    name_table = font["name"]
    for name_id in _REPLACED_NAME_IDS:
        name_table.removeNames(nameID=name_id)
    values = {
        1: family,
        2: style,
        3: f"{family} 2.37 {style}",
        4: f"{family} {style}",
        6: postscript,
        16: family,
        17: style,
    }
    for name_id, value in values.items():
        name_table.setName(value, name_id, 3, 1, 0x409)
    font.save(args.output, reorderTables=True)


if __name__ == "__main__":
    main()
