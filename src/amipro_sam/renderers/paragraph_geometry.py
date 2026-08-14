"""Shared paragraph-region geometry for layout-capable renderers."""

from __future__ import annotations

from dataclasses import dataclass

from ..model import Paragraph

_SOURCE_ROUNDING_TOLERANCE_TWIPS = 3


@dataclass(frozen=True, slots=True)
class ParagraphRegionMargins:
    """Resolved base margins for a source ``x, width`` paragraph region."""

    left_twips: int
    right_twips: int
    first_line_twips: int = 0


def resolve_paragraph_region(
    paragraph: Paragraph,
    container_width_twips: int | None,
) -> ParagraphRegionMargins | None:
    """Resolve a source paragraph region against an explicit container width.

    Ami Pro commonly rounds a full-width region a few twips beyond its page
    body.  That small excess is treated as a zero right margin.  Missing,
    non-integral, negative, empty, or materially overflowing geometry is not
    applied: callers retain their ordinary paragraph/style indentation.
    """

    x = getattr(paragraph, "region_x_twips", None)
    width = getattr(paragraph, "region_width_twips", None)
    if not all(type(value) is int for value in (x, width, container_width_twips)):
        return None
    assert isinstance(x, int)
    assert isinstance(width, int)
    assert isinstance(container_width_twips, int)
    if x < 0 or width <= 0 or container_width_twips <= 0:
        return None
    # In the dominant corpus form the measure is the complete container width
    # and x is the first-line position.  Applying that measure as a left indent
    # caused the historical right-edge sliver.
    if abs(width - container_width_twips) <= _SOURCE_ROUNDING_TOLERANCE_TWIPS:
        return ParagraphRegionMargins(
            left_twips=0,
            right_twips=0,
            first_line_twips=x,
        )

    end = x + width
    if x >= container_width_twips or end > (
        container_width_twips + _SOURCE_ROUNDING_TOLERANCE_TWIPS
    ):
        return None
    return ParagraphRegionMargins(
        left_twips=x,
        right_twips=max(0, container_width_twips - end),
    )
