"""Decide when source-container labels are preservation warnings."""

from __future__ import annotations

from ..model import (
    Footer,
    Frame,
    Header,
    _frame_structure_is_known,
    _furniture_structure_is_known,
)


def frame_structure_is_known(frame: object) -> bool:
    """Return whether a frame has enough validated structure to label silently."""

    return isinstance(frame, Frame) and _frame_structure_is_known(frame)


def furniture_structure_is_known(furniture: object) -> bool:
    """Return whether a header/footer is a valid source container."""

    return isinstance(furniture, Header | Footer) and _furniture_structure_is_known(
        furniture
    )


def show_container_label(container: object, *, requested: bool) -> bool:
    """Keep labels on request or whenever structure remains uncertain."""

    if requested:
        return True
    if isinstance(container, Frame):
        return not frame_structure_is_known(container)
    if isinstance(container, Header | Footer):
        if isinstance(container.blocks, list | tuple) and not container.blocks:
            return False
        return not furniture_structure_is_known(container)
    return True
