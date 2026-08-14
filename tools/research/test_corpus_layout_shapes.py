from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

import corpus_layout_shapes as shapes  # noqa: E402
from corpus_layout_shapes import CorpusAnalysisError, analyze_corpus, main  # noqa: E402


def _invented_sam(body: str, *, extra: str = "") -> bytes:
    layout = """[lay]
	Invented Layout
	1
	[rght]
		3000
		2000
		0
		500
		500
		1
		500
		500
		0
"""
    source = (
        "[ver]\n\t4\n[sty]\n\t\n"
        + layout
        + extra
        + "[edoc]\n"
        + body
        + "\n>\n"
    )
    return source.replace("\n", "\r\n").encode("cp1252")


def test_report_contains_only_aggregate_layout_shapes(tmp_path: Path) -> None:
    private_root = tmp_path / "private-name-must-not-escape"
    private_root.mkdir()
    private_text = "PRIVATE-TEXT-MUST-NOT-ESCAPE"
    body = "\n\n".join(
        f"<:I504,0,0,0><:#284,1005><:f240,Invented,>{private_text}{index}"
        for index in range(5)
    )
    (private_root / "private-document-name.sam").write_bytes(_invented_sam(body))
    (private_root / "excluded.doc").write_bytes(b"not selected")

    report = analyze_corpus(private_root)
    assert report == analyze_corpus(private_root)
    rendered = json.dumps(report, sort_keys=True)

    assert report["sample"]["selected_regular_files"] == 1
    assert report["sample"]["skipped_nonmatching_regular_files"] == 1
    assert report["paragraph_region"]["bounded_arity_two_command_count"] == 5
    assert report["four_field_I_command"]["bounded_arity_four_command_count"] == 5
    assert report["paragraph_region"]["page_body_comparison"][
        "five_but_not_three_twips"
    ] == 5
    assert report["four_field_I_command"]["blank_delimited_unit_association"][
        "commands_before_all_material_characters"
    ] == 5
    assert str(private_root) not in rendered
    assert "private-document-name" not in rendered
    assert private_text not in rendered
    assert "Invented Layout" not in rendered


def test_uncommon_bounded_commands_remain_observations(tmp_path: Path) -> None:
    private_root = tmp_path / "corpus"
    private_root.mkdir()
    body = "<:#1,0><:#opaque><:I1,2,3,4><:Iopaque>SAFE"
    (private_root / "sample.sam").write_bytes(_invented_sam(body))

    report = analyze_corpus(private_root)

    assert report["paragraph_region"]["bounded_arity_two_command_count"] == 1
    assert report["paragraph_region"]["second_field"]["zero_count"] == 1
    assert report["paragraph_region"]["shape_counts"] == {
        "bounded_exact_arity": 1,
        "not_bounded_exact_arity": 1,
        "observed_arity_1": 1,
        "observed_arity_2": 1,
    }
    assert report["four_field_I_command"]["bounded_arity_four_command_count"] == 1
    assert report["four_field_I_command"]["fields"][3]["zero_count"] == 0
    assert report["four_field_I_command"]["shape_counts"] == {
        "bounded_exact_arity": 1,
        "not_bounded_exact_arity": 1,
        "observed_arity_1": 1,
        "observed_arity_4": 1,
    }
    assert "opaque" not in json.dumps(report)


def test_symlink_inputs_are_not_followed(tmp_path: Path) -> None:
    private_root = tmp_path / "corpus"
    private_root.mkdir()
    target = tmp_path / "outside.sam"
    target.write_bytes(_invented_sam("<:#284,1000>TEXT"))
    (private_root / "linked.sam").symlink_to(target)

    report = analyze_corpus(private_root)

    assert report["sample"]["selected_regular_files"] == 0
    assert report["sample"]["skipped_symlinks"] == 1
    assert report["paragraph_region"]["bounded_arity_two_command_count"] == 0


def test_cli_does_not_echo_input_path_or_text(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    private_root = tmp_path / "private-cli-path"
    private_root.mkdir()
    private_text = "PRIVATE-CLI-TEXT"
    (private_root / "private-cli-name.sam").write_bytes(
        _invented_sam(f"<:#284,1000>{private_text}")
    )

    assert main([str(private_root)]) == 0
    captured = capsys.readouterr()

    assert str(private_root) not in captured.out
    assert "private-cli-name" not in captured.out
    assert private_text not in captured.out
    assert captured.err == ""


def test_table_shapes_bounds_merges_and_field_correlations(tmp_path: Path) -> None:
    private_root = tmp_path / "private-table-root"
    private_root.mkdir()
    private_cell_text = "PRIVATE-CELL-TEXT"
    private_formula_text = "PRIVATE-FORMULA-TEXT"
    table = f"""[frm]
	1
	524288
	0
	0
	1583
	763
	0
	0
	0
	[tbl]
		2 2 300 10 600 20 4 43 43
		[h]
			0 300 10 16 0 0 0
			1 400 10 2 0 0 0
		[e]
		[w]
			0 600 20 2 0
			1 900 20 2 0
		[e]
		[data]
			0 0 384 1 2 0 4369 0 0 0 0 0
>
			0 1 128 0 1 0 17 0 0 0 0 0
>
			1 0 8 0 0 11 1 1 0 14803425 0 0
<+@>{private_cell_text}
>
{private_formula_text}
			1 1 16 0 0 0 4096 1 1 0 0 0
<+A>right
>
		[tble]
"""
    (private_root / "private-table-name.sam").write_bytes(
        _invented_sam("body", extra=table)
    )

    report = analyze_corpus(private_root)
    tables = report["tables"]
    rendered = json.dumps(report, sort_keys=True)

    assert tables["table_marker_count"] == 1
    assert tables["data_marker_count"] == 1
    assert {
        family: tables["record_shapes"][family]["canonical_exact_count"]
        for family in ("tbl", "h", "w", "data")
    } == {"tbl": 1, "h": 2, "w": 2, "data": 4}
    coordinates = tables["coordinate_bounds"]
    assert coordinates["declared_cell_capacity"] == 4
    assert coordinates["records_outside_declared_grid"] == 0
    assert coordinates["tables_with_complete_rectangular_coordinate_set"] == 1
    swapped = coordinates["swapped_declared_grid_check"]
    assert swapped["records_inside_swapped_declared_grid"] == 4
    assert swapped["records_outside_swapped_declared_grid"] == 0
    assert swapped["tables_with_all_records_inside_swapped_declared_grid"] == 1
    assert swapped["different_declared_dimensions"]["records_compared"] == 0
    row_correlations = tables["row_h_records"]["default_correlations"]
    assert row_correlations["dimension_equals_table_default"] == 1
    assert row_correlations["dimension_differs_from_table_default"] == 1
    assert row_correlations["gutter_equals_table_default"] == 2
    assert row_correlations["data_bearing_records_compared"] == 2
    assert row_correlations["data_bearing_dimension_equals_table_default"] == 1
    tails = tables["tails_and_frame_extent_correlation"]
    assert tails["definitions_with_equal_field7_and_field8"] == 1
    assert tails["outer_height_minus_effective_rows"]["equals_field7"] == 1
    assert tails["outer_width_minus_effective_columns"]["equals_field7"] == 1
    merges = tables["merge_topology"]
    assert merges["anchor_bit_0x100_records"] == 1
    assert merges["member_bit_0x80_without_anchor_records"] == 1
    assert merges["raw_body_empty_validation"][
        "candidate_anchors_forming_complete_bounded_rectangles"
    ] == 1
    assert merges["raw_body_empty_validation"][
        "member_records_covered_by_complete_rectangles"
    ] == 1
    cell_fields = tables["data_cell_fields"]
    assert cell_fields["inline_alignment_low_flag_correlation"][
        "expected_low_flag_matches"
    ] == 2
    assert cell_fields["fields5_through11"][0]["maximum"] == 11
    assert cell_fields["fields5_through11"][4]["maximum"] == 14803425
    assert cell_fields["field7_material_presence"]["binary_field_records"] == 4
    assert cell_fields["field8_binary_pattern"]["one_count"] == 1
    assert cell_fields["post_close_metadata"]["nonblank_post_close_line_count"] == 1
    assert str(private_root) not in rendered
    assert "private-table-name" not in rendered
    assert private_cell_text not in rendered
    assert private_formula_text not in rendered


def test_swapped_table_orientation_is_counted_without_materializing_grids(
    tmp_path: Path,
) -> None:
    private_root = tmp_path / "orientation-corpus"
    private_root.mkdir()

    def frame(rows: int, columns: int, cell_row: int, cell_column: int) -> str:
        row_records = "\n".join(
            f"\t\t\t{index} 300 10 0 0 0 0" for index in range(rows)
        )
        column_records = "\n".join(
            f"\t\t\t{index} 600 20 0 0" for index in range(columns)
        )
        return f"""[frm]
\t1
\t0
\t0
\t0
\t2000
\t2000
\t[tbl]
\t\t{rows} {columns} 300 10 600 20 0 43 43
\t\t[h]
{row_records}
\t\t[e]
\t\t[w]
{column_records}
\t\t[e]
\t\t[data]
\t\t\t{cell_row} {cell_column} 0 0 0 0 0 0 0 0 0 0
>
\t\t[tble]
"""

    tables = frame(2, 3, 1, 2) + frame(3, 2, 2, 1)
    (private_root / "sample.sam").write_bytes(_invented_sam("body", extra=tables))

    report = analyze_corpus(private_root)["tables"]
    coordinates = report["coordinate_bounds"]
    swapped = coordinates["swapped_declared_grid_check"]
    assert coordinates["records_inside_declared_grid"] == 2
    assert swapped["records_compared"] == 2
    assert swapped["records_inside_swapped_declared_grid"] == 0
    assert swapped["records_outside_swapped_declared_grid"] == 2
    assert swapped["tables_with_cell_records_compared"] == 2
    assert swapped["tables_with_all_records_inside_swapped_declared_grid"] == 0
    assert swapped["tables_with_any_record_outside_swapped_declared_grid"] == 2
    assert swapped["different_declared_dimensions"]["records_compared"] == 2

    row_opposite = report["row_h_records"]["opposite_declared_dimension_check"]
    assert row_opposite["opposite_dimension"] == "declared_columns"
    assert row_opposite["records_compared"] == 5
    assert row_opposite["records_with_index_inside_opposite_declared_bound"] == 4
    assert row_opposite["records_with_index_outside_opposite_declared_bound"] == 1
    assert row_opposite["tables_with_records_compared"] == 2
    assert row_opposite["tables_with_all_indexes_inside_opposite_declared_bound"] == 1
    assert row_opposite["tables_with_any_index_outside_opposite_declared_bound"] == 1
    assert row_opposite["different_declared_dimensions"]["records_compared"] == 5

    column_opposite = report["column_w_records"][
        "opposite_declared_dimension_check"
    ]
    assert column_opposite["opposite_dimension"] == "declared_rows"
    assert column_opposite["records_compared"] == 5
    assert column_opposite["records_with_index_inside_opposite_declared_bound"] == 4
    assert column_opposite["records_with_index_outside_opposite_declared_bound"] == 1
    assert column_opposite["tables_with_records_compared"] == 2
    assert column_opposite[
        "tables_with_all_indexes_inside_opposite_declared_bound"
    ] == 1
    assert column_opposite[
        "tables_with_any_index_outside_opposite_declared_bound"
    ] == 1
    assert column_opposite["different_declared_dimensions"]["records_compared"] == 5


def test_orientation_denominators_exclude_empty_and_undefined_tables(
    tmp_path: Path,
) -> None:
    private_root = tmp_path / "orientation-denominator-corpus"
    private_root.mkdir()
    missing_definition = """[frm]
\t1
\t0
\t0
\t0
\t1000
\t1000
\t[tbl]
\t\tnot-a-definition
\t\t[h]
\t\t\t0 300 10 0 0 0 0
\t\t[e]
\t\t[w]
\t\t\t0 600 20 0 0
\t\t[e]
\t\t[data]
\t\t\t0 0 0 0 0 0 0 0 0 0 0 0
>
\t\t[tble]
"""
    empty_defined_table = """[frm]
\t1
\t0
\t0
\t0
\t1000
\t1000
\t[tbl]
\t\t2 3 300 10 600 20 0 43 43
\t\t[h]
\t\t[e]
\t\t[w]
\t\t[e]
\t\t[data]
\t\t[tble]
"""
    (private_root / "sample.sam").write_bytes(
        _invented_sam("body", extra=missing_definition + empty_defined_table)
    )

    report = analyze_corpus(private_root)["tables"]
    coordinates = report["coordinate_bounds"]
    assert coordinates["canonical_cell_record_count"] == 1
    assert coordinates["tables_compared"] == 1
    assert coordinates["cell_records_compared"] == 0
    assert coordinates[
        "cell_records_not_compared_without_canonical_definition"
    ] == 1
    assert coordinates["tables_with_cell_records_compared"] == 0
    assert coordinates["tables_with_all_records_inside_declared_grid"] == 0
    assert coordinates["swapped_declared_grid_check"]["records_compared"] == 0
    assert coordinates["swapped_declared_grid_check"][
        "tables_with_cell_records_compared"
    ] == 0

    for family in ("row_h_records", "column_w_records"):
        records = report[family]
        declared = records["declared_bound_checks"]
        opposite = records["opposite_declared_dimension_check"]
        assert records["canonical_record_count"] == 1
        assert declared["tables_with_canonical_definition"] == 1
        assert declared["records_compared"] == 0
        assert declared["records_not_compared_without_canonical_definition"] == 1
        assert declared["records_with_index_outside_declared_bound"] == 0
        assert declared["tables_with_records_compared"] == 0
        assert declared["tables_with_all_indexes_inside_declared_bound"] == 0
        assert opposite["records_compared"] == 0
        assert opposite["tables_with_records_compared"] == 0
        defaults = records["default_correlations"]
        assert defaults["records_compared"] == 0
        assert defaults["records_not_compared_without_canonical_definition"] == 1
        assert defaults["dimension_differs_from_table_default"] == 0
        assert defaults["gutter_differs_from_table_default"] == 0


def test_caps_fire_before_unbounded_result_growth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    private_root = tmp_path / "bounded-corpus"
    private_root.mkdir()
    (private_root / "sample.sam").write_bytes(
        _invented_sam("\n\n".join(f"<:#{value},1000>x" for value in range(3)))
    )

    monkeypatch.setattr(shapes, "MAX_COMMANDS", 2)
    with pytest.raises(CorpusAnalysisError, match="inline-command count"):
        analyze_corpus(private_root)


def test_distinct_value_cap_is_enforced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    private_root = tmp_path / "distinct-corpus"
    private_root.mkdir()
    (private_root / "sample.sam").write_bytes(
        _invented_sam("\n\n".join(f"<:#{value},1000>x" for value in range(3)))
    )

    monkeypatch.setattr(shapes, "MAX_DISTINCT_VALUES", 2)
    with pytest.raises(CorpusAnalysisError, match="distinct-value cap"):
        analyze_corpus(private_root)


def test_directory_entry_cap_is_checked_while_enumerating(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    private_root = tmp_path / "entry-corpus"
    private_root.mkdir()
    for index in range(3):
        (private_root / f"sample-{index}.sam").write_bytes(_invented_sam("body"))

    monkeypatch.setattr(shapes, "MAX_DIRECTORY_ENTRIES", 2)
    with pytest.raises(CorpusAnalysisError, match="directory-entry count"):
        analyze_corpus(private_root)


def test_blank_boundary_cap_is_checked_while_scanning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    private_root = tmp_path / "boundary-corpus"
    private_root.mkdir()
    (private_root / "sample.sam").write_bytes(_invented_sam("one\n\ntwo\n\nthree"))

    monkeypatch.setattr(shapes, "MAX_BLANK_BOUNDARIES", 1)
    with pytest.raises(CorpusAnalysisError, match="blank-line boundaries"):
        analyze_corpus(private_root)


def test_oversized_declared_grid_is_not_materialized(tmp_path: Path) -> None:
    private_root = tmp_path / "large-grid-corpus"
    private_root.mkdir()
    table = """[frm]
	1
	0
	0
	0
	1000
	1000
	[tbl]
		999999999 999999999 1 0 1 0 0 0 0
	[data]
			0 0 0 0 0 0 0 0 0 0 0 0
>
	[tble]
"""
    (private_root / "sample.sam").write_bytes(_invented_sam("body", extra=table))

    report = analyze_corpus(private_root)

    assert report["tables"]["table_definition"]["canonical_count"] == 1
    assert report["tables"]["coordinate_bounds"][
        "tables_eligible_for_complete_rectangular_set_check"
    ] == 0
    assert report["tables"]["tails_and_frame_extent_correlation"][
        "tables_skipped_by_grid_caps"
    ] == 1


def test_table_grid_work_cap_precedes_full_set_materialization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    private_root = tmp_path / "grid-work-corpus"
    private_root.mkdir()
    table = """[frm]
	1
	0
	0
	0
	1000
	1000
	[tbl]
		2 2 1 0 1 0 0 0 0
	[data]
			0 0 0 0 0 0 0 0 0 0 0 0
>
	[tble]
"""
    (private_root / "sample.sam").write_bytes(_invented_sam("body", extra=table))

    monkeypatch.setattr(shapes, "MAX_TABLE_GRID_COORDINATE_WORK", 3)
    with pytest.raises(CorpusAnalysisError, match="full-grid checks"):
        analyze_corpus(private_root)
