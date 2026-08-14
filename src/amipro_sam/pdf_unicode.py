"""Deterministic Unicode text support for the PDF renderer.

The public ReportLab RTL interface imports a small module named ``rlbidi``.
That module is not available from the public Python Package Index, so the
toolkit supplies the narrow ``log2vis`` interface ReportLab needs by adapting
the public, LGPL-licensed :mod:`python-bidi` implementation.  The adapter is
installed before ReportLab's text modules are imported.

Font data is always read from this package into memory.  A SAM-supplied font
name is only a family hint; it can never become a filesystem or network path.
"""

from __future__ import annotations

import importlib
import math
import re
import sys
import threading
import types
import unicodedata
from dataclasses import dataclass, field
from functools import cache
from importlib.resources import files
from io import BytesIO
from typing import Any

from bidi.algorithm import (
    PARAGRAPH_LEVELS,
    apply_mirroring,
    explicit_embed_and_overrides,
    get_base_level,
    get_embedding_levels,
    get_empty_storage,
    reorder_resolved_levels,
    resolve_implicit_levels,
    resolve_neutral_types,
    resolve_weak_types,
)

_FONT_LOCK = threading.Lock()
_FONTS_READY = False
_REGISTERED_FONTS: dict[str, object] = {}
_FONT_PACKAGE = "assets/fonts"
_REPLACEMENT = "\ufffd"
_PDF_TEXT_LIMIT = 4_000_000
_PARAGRAPH_LIMIT = 65_536
_TOKEN_LIMIT = 1_024
_UNIQUE_LIMIT = 8_192
_COMBINING_LIMIT = 64
_BIDI_CONTROL_LIMIT = 4_096
_FONT_SPAN_LIMIT = 4_096
_MIN_TRACKED_TEXT_ALIAS = 4_096
_OMITTED_TEXT = "[PDF text omitted at safe Unicode limit]"
_REPEATED_OMITTED_TEXT = "[Repeated PDF text omitted at safe Unicode limit]"
_OMITTED_TOKEN = "[overlong token omitted at safe PDF limit]"
_OMITTED_SPANS = "[font fallback spans omitted at safe PDF limit]"
_SUPPORTED_BIDI_CONTROLS = frozenset(
    {
        0x200C,  # ZWNJ: shaping control
        0x200D,  # ZWJ: shaping control
        0x200E,  # LRM
        0x200F,  # RLM
        0x202A,  # LRE
        0x202B,  # RLE
        0x202C,  # PDF
        0x202D,  # LRO
        0x202E,  # RLO
        0xFEFF,  # zero-width no-break space
    }
)
_NONSPACE_TOKEN = re.compile(r"\S+")
_MIXED_TOKEN_REPLACEMENT = "�"


FONT_FILES: tuple[tuple[str, str], ...] = (
    ("AmiProSans", "DejaVuSans.ttf"),
    ("AmiProSans-Bold", "DejaVuSans-Bold.ttf"),
    ("AmiProSans-Oblique", "DejaVuSans-Oblique.ttf"),
    ("AmiProSans-BoldOblique", "DejaVuSans-BoldOblique.ttf"),
    ("AmiProCJK", "AmiProPreservationCJK-Regular.ttf"),
)


def _python_bidi_log2vis(
    text: str | bytes,
    base_direction: str | None = None,
    clean: bool = True,
    positions_V_to_L: list[int] | None = None,
    direction: str | None = None,
    **_options: object,
) -> str | bytes:
    """Return visual order with the index map expected by ReportLab.

    ``python-bidi`` deliberately exposes its intermediate UAX #9 stages.  We
    attach a logical index before reordering so ReportLab can keep shaped word
    fragments associated with their source positions.  ReportLab always asks
    for cleaned output; ``clean`` remains accepted for API compatibility.
    """

    del clean
    was_bytes = isinstance(text, bytes)
    logical = text.decode("utf-8", errors="replace") if was_bytes else text
    requested = base_direction or direction
    if requested:
        requested = {"LTR": "L", "RTL": "R"}.get(requested.upper(), requested.upper())
        if requested not in PARAGRAPH_LEVELS:
            requested = None

    storage = get_empty_storage()
    level = get_base_level(logical) if requested is None else PARAGRAPH_LEVELS[requested]
    storage["base_level"] = level
    storage["base_dir"] = ("L", "R")[level]
    get_embedding_levels(logical, storage, debug=False)
    for logical_index, item in enumerate(storage["chars"]):
        item["logical_index"] = logical_index
    explicit_embed_and_overrides(storage, debug=False)
    resolve_weak_types(storage, debug=False)
    resolve_neutral_types(storage, debug=False)
    resolve_implicit_levels(storage, debug=False)
    reorder_resolved_levels(storage, debug=False)
    apply_mirroring(storage, debug=False)
    if positions_V_to_L is not None:
        positions_V_to_L.extend(item["logical_index"] for item in storage["chars"])
    result = "".join(item["ch"] for item in storage["chars"])
    return result.encode("utf-8") if was_bytes else result


def _logical_word_visual_order(
    words: list[str] | tuple[str, ...],
    direction: str = "RTL",
    clean: bool = True,
    wx: bool = False,
) -> list[object]:
    """Map logical whitespace-delimited words to visual positions exactly once.

    ReportLab's stock helper assigns a visual word to a logical word by the
    first matching character. Paired punctuation can make two logical words
    resolve to the same visual index. Deriving the index from every character's
    preserved logical position avoids duplicated/dropped LTR spans.
    """

    del clean
    import reportlab.pdfgen.textobject as textobject

    raw = " ".join(words)
    positions: list[int] = []
    visual = _python_bidi_log2vis(
        raw,
        base_direction=direction,
        positions_V_to_L=positions,
    )
    visual_position_for_logical = {
        logical: visual_index for visual_index, logical in enumerate(positions)
    }
    raw_matches = list(textobject.wordpat.finditer(raw))
    visual_matches = list(textobject.wordpat.finditer(visual))
    visual_word_for_character: dict[int, int] = {}
    for visual_word, match in enumerate(visual_matches):
        for visual_index in range(*match.span()):
            visual_word_for_character[positions[visual_index]] = visual_word

    mapped: list[tuple[int, int, str]] = []
    for logical_word, match in enumerate(raw_matches):
        candidates = [
            (
                visual_word_for_character[logical_index],
                visual_position_for_logical[logical_index],
            )
            for logical_index in range(*match.span())
            if logical_index in visual_word_for_character
        ]
        if not candidates:
            continue
        visual_word = min(candidates, key=lambda item: item[1])[0]
        mapped.append((visual_word, logical_word, visual_matches[visual_word].group(0)))

    mapped.sort(key=lambda item: item[0])
    if wx:
        return [logical_word for _visual, logical_word, _text in mapped]
    return [
        textobject.BidiStr(text, visual_word, logical_word)
        for visual_word, logical_word, text in mapped
    ]


def _install_reportlab_bidi_adapter() -> None:
    adapter = types.ModuleType("rlbidi")
    adapter.log2vis = _python_bidi_log2vis  # type: ignore[attr-defined]
    adapter.__doc__ = "python-bidi adapter installed by amipro-sam-toolkit"
    sys.modules["rlbidi"] = adapter


_install_reportlab_bidi_adapter()

# ReportLab binds bidi support at module-import time.  Applications commonly
# import Platypus before importing this toolkit, so repair that cached binding
# explicitly instead of silently falling back to unshaped logical order.
_existing_textobject = sys.modules.get("reportlab.pdfgen.textobject")
if _existing_textobject is not None and not getattr(
    _existing_textobject, "rtlSupport", False
):
    _existing_textobject = importlib.reload(_existing_textobject)

# These imports must remain below the adapter installation.
from reportlab.lib.colors import Color  # noqa: E402
from reportlab.pdfbase import pdfmetrics  # noqa: E402
from reportlab.pdfbase.ttfonts import TTFont, uharfbuzz  # noqa: E402
from reportlab.pdfgen.textobject import bidiShapedText  # noqa: E402
from reportlab.platypus import Flowable  # noqa: E402

_existing_canvas = sys.modules.get("reportlab.pdfgen.canvas")
if _existing_canvas is not None:
    _existing_canvas.bidiShapedText = bidiShapedText

# Replace the paired-punctuation-sensitive ReportLab word mapper while retaining
# its public BidiStr interface and shaping pipeline.
import reportlab.pdfgen.textobject as _reportlab_textobject  # noqa: E402

_reportlab_textobject.bidiWordList = _logical_word_visual_order
bidiShapedText = _reportlab_textobject.bidiShapedText
if _existing_canvas is not None:
    _existing_canvas.bidiShapedText = bidiShapedText


class UnicodePdfError(RuntimeError):
    """Raised when the fixed Unicode PDF stack is unavailable."""


@dataclass(slots=True)
class PdfTextBudget:
    """Cumulative, renderer-owned limits applied before shaping/subsetting."""

    remaining: int = _PDF_TEXT_LIMIT
    seen_codepoints: set[int] = field(default_factory=set)
    bidi_controls: int = 0
    exhausted_marker_emitted: bool = False
    repeated_alias_marker_emitted: bool = False
    seen_large_text_ids: set[int] = field(default_factory=set)
    seen_blocks: set[int] = field(default_factory=set)

    def prepare(
        self,
        value: object,
        *,
        paragraph_limit: int = _PARAGRAPH_LIMIT,
        unit_boundary: bool = False,
    ) -> str:
        if isinstance(value, bytes):
            value = value.decode("utf-8", errors="replace")
        if not isinstance(value, str):
            return ""
        if len(value) >= _MIN_TRACKED_TEXT_ALIAS:
            identity = id(value)
            if identity in self.seen_large_text_ids:
                if self.repeated_alias_marker_emitted:
                    return ""
                self.repeated_alias_marker_emitted = True
                if len(_REPEATED_OMITTED_TEXT) <= self.remaining:
                    self.remaining -= len(_REPEATED_OMITTED_TEXT)
                else:
                    self.remaining = 0
                return _REPEATED_OMITTED_TEXT
            self.seen_large_text_ids.add(identity)
        text = _bounded_unit(value, paragraph_limit) if unit_boundary else value
        text = _sanitize_scalars(text, self)
        text = _bound_tokens(text)
        if len(text) <= self.remaining:
            self.remaining -= len(text)
            return text
        if self.remaining <= 0:
            if self.exhausted_marker_emitted:
                return ""
            self.exhausted_marker_emitted = True
            return _OMITTED_TEXT
        marker = " " + _OMITTED_TEXT
        keep = max(0, self.remaining - len(marker))
        result = text[:keep] + marker
        self.remaining = 0
        self.exhausted_marker_emitted = True
        return result


def ensure_pdf_fonts() -> None:
    """Register the fixed in-package TrueType inventory in deterministic order."""

    global _FONTS_READY
    if _FONTS_READY and all(
        pdfmetrics.getFont(name) is registered
        for name, registered in _REGISTERED_FONTS.items()
    ):
        return
    with _FONT_LOCK:
        if _FONTS_READY and all(
            pdfmetrics.getFont(name) is registered
            for name, registered in _REGISTERED_FONTS.items()
        ):
            return
        if uharfbuzz is None:
            raise UnicodePdfError("uharfbuzz is required for Unicode PDF shaping")
        root = files("amipro_sam").joinpath(_FONT_PACKAGE)
        for internal_name, filename in FONT_FILES:
            try:
                payload = root.joinpath(filename).read_bytes()
            except (FileNotFoundError, OSError) as error:
                raise UnicodePdfError(f"bundled PDF font is unavailable: {filename}") from error
            candidate = TTFont(
                internal_name, BytesIO(payload), validate=1, shapable=True
            )
            pdfmetrics.registerFont(candidate)
            registered = pdfmetrics.getFont(internal_name)
            if registered is not candidate:
                raise UnicodePdfError(
                    f"ReportLab font registry name collision: {internal_name}"
                )
            _REGISTERED_FONTS[internal_name] = registered
        _coverage.cache_clear()
        _FONTS_READY = True


def unicode_font_name(requested_family: object, *, bold: bool, italic: bool) -> str:
    # Source family names remain untrusted presentation hints.  A single fixed
    # family avoids host-font discovery while preserving bold/italic intent.
    del requested_family
    suffix = {
        (False, False): "",
        (True, False): "-Bold",
        (False, True): "-Oblique",
        (True, True): "-BoldOblique",
    }[(bold, italic)]
    return "AmiProSans" + suffix


def unicode_font_spans(text: str, preferred_font: str) -> list[tuple[str, str]]:
    """Coalesce text into fixed-font spans and replace unsupported scalars."""

    ensure_pdf_fonts()
    primary = _coverage(preferred_font)
    regular = _coverage("AmiProSans")
    cjk = _coverage("AmiProCJK")
    spans: list[tuple[str, str]] = []
    current_font = preferred_font
    current: list[str] = []
    for character in text:
        codepoint = ord(character)
        if character in "\n\t" or codepoint in _SUPPORTED_BIDI_CONTROLS:
            selected = current_font
            rendered = character
        elif codepoint in primary:
            selected = preferred_font
            rendered = character
        elif codepoint in regular:
            selected = "AmiProSans"
            rendered = character
        elif codepoint in cjk:
            selected = "AmiProCJK"
            rendered = character
        else:
            selected = preferred_font
            rendered = _REPLACEMENT
        if current and selected != current_font:
            if len(spans) >= _FONT_SPAN_LIMIT - 1:
                spans.append(("AmiProSans", _OMITTED_SPANS))
                return spans
            spans.append((current_font, "".join(current)))
            current = []
        current_font = selected
        current.append(rendered)
    if current:
        spans.append((current_font, "".join(current)))
    return spans


def contains_rtl(text: str) -> bool:
    return any(unicodedata.bidirectional(character) in {"R", "AL"} for character in text)


def base_direction(text: str) -> str:
    for character in text:
        value = unicodedata.bidirectional(character)
        if value in {"R", "AL"}:
            return "RTL"
        if value == "L":
            return "LTR"
    return "LTR"


def rtl_font_name(text: str, preferred_font: str) -> str:
    """Select one shaping-capable face for the whole bidi paragraph."""

    ensure_pdf_fonts()
    significant = {
        ord(character)
        for character in text
        if character not in "\n\t" and ord(character) not in _SUPPORTED_BIDI_CONTROLS
    }
    if significant <= _coverage(preferred_font):
        return preferred_font
    # DejaVu's oblique faces intentionally have narrower Arabic coverage.
    # Flatten style to regular rather than substituting missing glyph boxes.
    return "AmiProSans"


class BidiTextFlowable(Flowable):
    """A bounded, splittable paragraph drawn through ReportLab's bidi canvas API.

    ReportLab 4.4 cannot combine Platypus multi-fragment shaping and bidi order.
    This conservative flowable therefore flattens inline styling for paragraphs
    containing strong RTL characters, while preserving logical text through a
    PDF ``ActualText`` span on every visual line.
    """

    def __init__(
        self,
        text: str,
        *,
        font_name: str,
        font_size: float,
        leading: float,
        text_color: Color,
        alignment: str | None = None,
        left_indent: float = 0.0,
        right_indent: float = 0.0,
        first_indent: float = 0.0,
        space_before: float = 0.0,
        space_after: float = 0.0,
        fixed_lines: list[str] | None = None,
    ) -> None:
        super().__init__()
        ensure_pdf_fonts()
        self.font_name = rtl_font_name(text, font_name)
        self.font_size = font_size
        self.leading = leading
        self.text_color = text_color
        self.alignment = alignment
        self.left_indent = left_indent
        self.right_indent = right_indent
        self.first_indent = first_indent
        self.spaceBefore = space_before
        self.spaceAfter = space_after
        self._logical_text = _rtl_supported_text(text, self.font_name)
        self._fixed_lines = fixed_lines
        self._lines: list[str] = []
        self._available_width = 0.0
        self.width = 0.0
        self.height = 0.0

    def getPlainText(self) -> str:  # noqa: N802 - ReportLab convention
        return self._logical_text

    def wrap(self, available_width: float, _available_height: float) -> tuple[float, float]:
        self.width = max(1.0, float(available_width))
        self._available_width = max(
            1.0, self.width - max(0.0, self.left_indent) - max(0.0, self.right_indent)
        )
        self._lines = (
            list(self._fixed_lines)
            if self._fixed_lines is not None
            else _wrap_bidi_text(
                self._logical_text,
                self.font_name,
                self.font_size,
                self._available_width,
            )
        )
        if not self._lines:
            self._lines = [""]
        self.height = self.leading * len(self._lines)
        return self.width, self.height

    def split(self, available_width: float, available_height: float) -> list[Flowable]:
        if not self._lines or not math.isclose(self.width, max(1.0, available_width)):
            self.wrap(available_width, available_height)
        count = int(max(0.0, available_height) // self.leading)
        if count <= 0:
            return []
        if count >= len(self._lines):
            return [self]
        return [
            self._with_lines(self._lines[:count], space_before=self.spaceBefore, space_after=0.0),
            self._with_lines(self._lines[count:], space_before=0.0, space_after=self.spaceAfter),
        ]

    def draw(self) -> None:
        canvas = self.canv
        canvas.saveState()
        try:
            canvas.setFont(self.font_name, self.font_size)
            canvas.setFillColor(self.text_color)
            y = self.height - self.leading + max(0.0, (self.leading - self.font_size) * 0.55)
            for index, line in enumerate(self._lines):
                direction = base_direction(line)
                default_alignment = "right" if direction == "RTL" else "left"
                alignment = (
                    self.alignment
                    if self.alignment in {"left", "right", "center"}
                    else default_alignment
                )
                first = self.first_indent if index == 0 else 0.0
                left = max(0.0, self.left_indent + (first if alignment == "left" else 0.0))
                right = self.width - max(
                    0.0, self.right_indent + (-first if alignment == "right" else 0.0)
                )
                canvas.addLiteral(
                    f"/Span <</ActualText <{_actual_text_hex(line)}>>> BDC"
                )
                if alignment == "center":
                    canvas.drawCentredString(
                        (left + right) / 2,
                        y,
                        line,
                        direction=direction,
                        shaping=True,
                    )
                elif alignment == "right":
                    canvas.drawRightString(
                        right,
                        y,
                        line,
                        direction=direction,
                        shaping=True,
                    )
                else:
                    canvas.drawString(
                        left,
                        y,
                        line,
                        direction=direction,
                        shaping=True,
                    )
                canvas.addLiteral("EMC")
                y -= self.leading
        finally:
            canvas.restoreState()

    def _with_lines(
        self, lines: list[str], *, space_before: float, space_after: float
    ) -> BidiTextFlowable:
        return BidiTextFlowable(
            "\n".join(lines),
            font_name=self.font_name,
            font_size=self.font_size,
            leading=self.leading,
            text_color=self.text_color,
            alignment=self.alignment,
            left_indent=self.left_indent,
            right_indent=self.right_indent,
            first_indent=self.first_indent,
            space_before=space_before,
            space_after=space_after,
            fixed_lines=lines,
        )


def draw_unicode_line(
    canvas: Any,
    text: str,
    *,
    x: float,
    y: float,
    font_name: str,
    font_size: float,
    max_width: float,
) -> None:
    """Draw one already-bounded furniture line with logical extraction text."""

    if contains_rtl(text):
        selected_font = rtl_font_name(text, font_name)
        logical = _rtl_supported_text(text, selected_font)
        direction = base_direction(logical)
        canvas.setFont(selected_font, font_size)
        canvas.addLiteral(f"/Span <</ActualText <{_actual_text_hex(logical)}>>> BDC")
        if direction == "RTL":
            canvas.drawRightString(
                x + max_width,
                y,
                logical,
                direction=direction,
                shaping=True,
            )
        else:
            canvas.drawString(x, y, logical, direction=direction, shaping=True)
        canvas.addLiteral("EMC")
        return

    cursor = x
    for selected_font, span in unicode_font_spans(text, font_name):
        canvas.setFont(selected_font, font_size)
        canvas.drawString(cursor, y, span)
        cursor += pdfmetrics.stringWidth(span, selected_font, font_size)


def unicode_line_width(text: str, font_name: str, font_size: float) -> float:
    if contains_rtl(text):
        logical = _rtl_supported_text(text, font_name)
        _shaped, width = bidiShapedText(
            logical,
            base_direction(logical),
            fontName=font_name,
            fontSize=font_size,
            shaping=True,
        )
        return float(width)
    return sum(
        pdfmetrics.stringWidth(span, selected_font, font_size)
        for selected_font, span in unicode_font_spans(text, font_name)
    )


def unicode_wrap_lines(text: str, font_name: str, font_size: float, width: float) -> list[str]:
    if contains_rtl(text):
        return _wrap_bidi_text(
            _rtl_supported_text(text, font_name), font_name, font_size, width
        )
    return _wrap_with_measure(
        text,
        width,
        lambda value: unicode_line_width(value, font_name, font_size),
    )


@cache
def _coverage(font_name: str) -> frozenset[int]:
    font = pdfmetrics.getFont(font_name)
    return frozenset(font.face.charWidths)


def _bounded_unit(text: str, maximum: int) -> str:
    maximum = max(1, min(int(maximum), _PARAGRAPH_LIMIT))
    if len(text) <= maximum:
        return text
    marker = " " + _OMITTED_TEXT + " "
    keep = max(0, maximum - len(marker))
    before = keep // 2
    after = keep - before
    return text[:before] + marker + (text[-after:] if after else "")


def _sanitize_scalars(text: str, budget: PdfTextBudget) -> str:
    result: list[str] = []
    combining = 0
    combining_omitted = False
    for character in text.replace("\r\n", "\n").replace("\r", "\n"):
        codepoint = ord(character)
        category = unicodedata.category(character)
        if character in "\n\t":
            rendered = character
            combining = 0
            combining_omitted = False
        elif (
            0xD800 <= codepoint <= 0xDFFF
            or codepoint > 0xFFFF
            or 0xFDD0 <= codepoint <= 0xFDEF
            or codepoint in {0xFFFE, 0xFFFF}
            or (category == "Cc")
            or (category == "Cf" and codepoint not in _SUPPORTED_BIDI_CONTROLS)
        ):
            rendered = _REPLACEMENT
            combining = 0
            combining_omitted = False
        else:
            if codepoint in _SUPPORTED_BIDI_CONTROLS:
                budget.bidi_controls += 1
                rendered = (
                    _REPLACEMENT
                    if budget.bidi_controls > _BIDI_CONTROL_LIMIT
                    else character
                )
            else:
                rendered = character
            if unicodedata.combining(character):
                combining += 1
                if combining > _COMBINING_LIMIT:
                    if combining_omitted:
                        continue
                    rendered = _REPLACEMENT
                    combining_omitted = True
            else:
                combining = 0
                combining_omitted = False

        rendered_codepoint = ord(rendered)
        if (
            rendered_codepoint not in budget.seen_codepoints
            and len(budget.seen_codepoints) >= _UNIQUE_LIMIT
        ):
            rendered = _REPLACEMENT
            rendered_codepoint = ord(rendered)
        budget.seen_codepoints.add(rendered_codepoint)
        result.append(rendered)
    return "".join(result)


def _bound_tokens(text: str) -> str:
    result: list[str] = []
    token_length = 0
    token_omitted = False
    for character in text:
        if character.isspace():
            token_length = 0
            token_omitted = False
            result.append(character)
            continue
        token_length += 1
        if token_length <= _TOKEN_LIMIT:
            result.append(character)
        elif not token_omitted:
            result.append(_OMITTED_TOKEN)
            token_omitted = True
    return "".join(result)


def _rtl_supported_text(text: str, font_name: str) -> str:
    coverage = _coverage(rtl_font_name(text, font_name))
    supported = "".join(
        character
        if (
            character in "\n\t"
            or
            ord(character) in coverage
            or ord(character) in _SUPPORTED_BIDI_CONTROLS
        )
        else _REPLACEMENT
        for character in text
    )
    return _NONSPACE_TOKEN.sub(_replace_mixed_direction_token, supported)


def _replace_mixed_direction_token(match: re.Match[str]) -> str:
    token = match.group(0)
    classes = {unicodedata.bidirectional(character) for character in token}
    if classes & {"R", "AL"} and classes & {"L", "EN", "AN"}:
        return _MIXED_TOKEN_REPLACEMENT
    return token


def _wrap_bidi_text(text: str, font_name: str, font_size: float, width: float) -> list[str]:
    def measure(value: str) -> float:
        _shaped, measured = bidiShapedText(
            value,
            base_direction(value),
            fontName=font_name,
            fontSize=font_size,
            shaping=True,
        )
        return float(measured)

    return _wrap_with_measure(text, width, measure)


def _wrap_with_measure(text: str, width: float, measure: Any) -> list[str]:
    width = max(1.0, float(width))
    result: list[str] = []
    for source_line in text.split("\n"):
        # A tab remains a visible bounded spacing unit in PDF reflow.  Preserve
        # leading/interior ASCII spaces rather than letting split/join erase
        # them from RTL ActualText.
        source_line = source_line.replace("\t", "    ")
        leading = source_line[: len(source_line) - len(source_line.lstrip(" "))]
        words = source_line[len(leading) :].split(" ")
        current = ""
        if leading:
            if measure(leading) <= width:
                current = leading
            else:
                fragments = _split_long_word(leading, width, measure)
                result.extend(fragments[:-1])
                current = fragments[-1] if fragments else ""
        for word in words:
            candidate = word if not current else current + ("" if current.isspace() else " ") + word
            if not current or measure(candidate) <= width:
                current = candidate
                if measure(current) <= width:
                    continue
            if current and current != word:
                result.append(current)
                current = ""
            if not word:
                continue
            if measure(word) <= width:
                current = word
                continue
            fragments = _split_long_word(word, width, measure)
            result.extend(fragments[:-1])
            current = fragments[-1] if fragments else ""
        result.append(current)
    return result or [""]


def _split_long_word(word: str, width: float, measure: Any) -> list[str]:
    clusters: list[str] = []
    for character in word:
        if clusters and unicodedata.combining(character):
            clusters[-1] += character
        else:
            clusters.append(character)
    result: list[str] = []
    start = 0
    while start < len(clusters):
        if measure(clusters[start]) > width:
            result.append(clusters[start])
            start += 1
            continue
        low = start + 1
        high = len(clusters)
        best = low
        while low <= high:
            middle = (low + high) // 2
            candidate = "".join(clusters[start:middle])
            if measure(candidate) <= width:
                best = middle
                low = middle + 1
            else:
                high = middle - 1
        result.append("".join(clusters[start:best]))
        start = best
    return result


def _actual_text_hex(text: str) -> str:
    return ("\ufeff" + text).encode("utf-16-be", errors="replace").hex().upper()
