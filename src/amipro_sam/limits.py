"""Centralized resource limits for untrusted legacy documents."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ParseLimits:
    """Conservative limits that callers may lower for hosted services."""

    max_file_bytes: int = 64 * 1024 * 1024
    max_line_bytes: int = 4 * 1024 * 1024
    max_lines: int = 1_000_000
    max_records: int = 1_000_000
    max_container_depth: int = 64
    max_styles: int = 10_000
    max_table_cells: int = 100_000
    max_embedded_asset_bytes: int = 16 * 1024 * 1024
    max_total_asset_bytes: int = 64 * 1024 * 1024
