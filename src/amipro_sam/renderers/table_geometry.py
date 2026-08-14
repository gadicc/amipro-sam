"""Bounded normalization of Ami Pro table column geometry."""

from __future__ import annotations

from ..model import Table, TableColumnDefinition, TableDefinition


def table_column_widths(
    table: object,
    column_count: int,
    container_width_twips: int,
    *,
    minimum_width_twips: int = 1,
) -> list[int]:
    """Scale source width weights to an exact output-container width.

    Invalid manually-created IR falls back to equal weights.  Source widths are
    treated as proportions because the destination container may not have the
    same page or frame geometry as Ami Pro.
    """

    if type(column_count) is not int or not 1 <= column_count <= 256:
        return []
    if type(container_width_twips) is not int or container_width_twips <= 0:
        return []
    if container_width_twips < column_count:
        return []
    minimum = (
        minimum_width_twips
        if type(minimum_width_twips) is int and minimum_width_twips > 0
        else 1
    )
    if minimum * column_count > container_width_twips:
        minimum = 1

    weights = [1] * column_count
    if isinstance(table, Table):
        definition = table.definition
        if isinstance(definition, TableDefinition):
            default = _weight(
                definition.default_column_width_twips,
                definition.default_column_gutter_twips,
            )
            if default is not None:
                weights = [default] * column_count
        columns = table.columns
        if isinstance(columns, list | tuple):
            seen: set[int] = set()
            for column in columns[:256]:
                if not isinstance(column, TableColumnDefinition):
                    continue
                index = column.index
                weight = _weight(column.width_twips, column.gutter_twips)
                if (
                    type(index) is int
                    and 0 <= index < column_count
                    and index not in seen
                    and weight is not None
                ):
                    weights[index] = weight
                    seen.add(index)

    distributable = container_width_twips - minimum * column_count
    if distributable <= 0:
        base, remainder = divmod(container_width_twips, column_count)
        return [base + (1 if index < remainder else 0) for index in range(column_count)]
    total_weight = sum(weights)
    scaled = [distributable * weight for weight in weights]
    additions = [value // total_weight for value in scaled]
    remainder = distributable - sum(additions)
    order = sorted(
        range(column_count),
        key=lambda index: (scaled[index] % total_weight, -index),
        reverse=True,
    )
    for index in order[:remainder]:
        additions[index] += 1
    return [minimum + value for value in additions]


def table_column_count(table: object) -> int:
    """Return a bounded logical column count for textual/HTML rendering."""

    if not isinstance(table, Table):
        return 0
    definition = table.definition
    if (
        isinstance(definition, TableDefinition)
        and type(definition.declared_columns) is int
        and 1 <= definition.declared_columns <= 256
    ):
        return definition.declared_columns
    maximum = 0
    rows = table.rows
    if not isinstance(rows, list | tuple):
        return 0
    for row in rows[:390]:
        cells = getattr(row, "cells", None)
        if not isinstance(cells, list | tuple):
            continue
        width = 0
        for cell in cells[:256]:
            span = getattr(cell, "column_span", 1)
            width += span if type(span) is int and 1 <= span <= 256 else 1
            if width >= 256:
                width = 256
                break
        maximum = max(maximum, width)
    return maximum


def _weight(width: object, gutter: object) -> int | None:
    if type(width) is not int or type(gutter) is not int:
        return None
    if not 0 <= width <= 32_767 or not 0 <= gutter <= 32_767:
        return None
    result = width + gutter
    return result if result > 0 else None
