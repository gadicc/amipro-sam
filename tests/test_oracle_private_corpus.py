from __future__ import annotations

import json
from pathlib import Path

import pytest

from amipro_oracle.compare import _normalize_text
from amipro_oracle.errors import OracleError
from amipro_oracle.io import atomic_write_json, digest_json, sha256_file
from amipro_oracle.native_batch import ANALYSIS_PROFILE, _bbox_pages
from amipro_oracle.private_corpus import (
    PRIVATE_CORPUS_AGGREGATE_SCHEMA,
    audit_aggregate,
    build_aggregate,
    select_native_documents,
)
from amipro_oracle.raster import encode_rgb_png, raster_difference


def _image_record() -> dict[str, object]:
    return {
        "schema": "amipro-oracle-image-v1",
        "provider": "podman",
        "platform": "linux/amd64",
        "image": "localhost/invented-oracle:test",
        "image_id": "a" * 64,
        "image_digest": f"sha256:{'b' * 64}",
        "lock_sha256": "c" * 64,
        "source_date_epoch": 1_700_000_000,
    }


def _artifact(path: Path, root: Path, kind: str) -> dict[str, object]:
    return {
        "kind": kind,
        "path": path.relative_to(root).as_posix(),
        "size": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def _invented_native_batch(tmp_path: Path) -> tuple[Path, Path, Path, dict[str, object]]:
    home = tmp_path / "oracle"
    source_root = tmp_path / "invented-source"
    batch = home / "private-batches" / "invented-batch"
    evidence = home / "jobs" / "batch-document-invented"
    for directory in (
        source_root,
        batch / "jobs" / "00001",
        batch / "reference-pdf",
        evidence / "output",
    ):
        directory.mkdir(parents=True, mode=0o700)
    batch.chmod(0o700)

    source = source_root / "invented.sam"
    source.write_bytes(b"[ver]\r\n\t4\r\n[edoc]\r\nINVENTED CONTENT ONLY\r\n")
    source_hash = sha256_file(source)
    pdf_payload = b"%PDF-1.4\n% invented test bytes only\n"
    reference_pdf = batch / "reference-pdf" / "invented.pdf"
    native_pdf = evidence / "output" / "document.pdf"
    reference_pdf.write_bytes(pdf_payload)
    native_pdf.write_bytes(pdf_payload)
    raster = evidence / "output" / "page-1.png"
    raster.write_bytes(b"invented raster placeholder")
    analysis = {
        "schema": "amipro-oracle-analysis-v1",
        "backend": "real",
        "profile": ANALYSIS_PROFILE,
        "page_count": 1,
        "pages": [
            {
                "number": 1,
                "width_pt": 612.0,
                "height_pt": 792.0,
                "text": "INVENTED CONTENT ONLY",
                "text_boxes": [
                    {
                        "text": "INVENTED",
                        "x0": 10.0,
                        "y0": 10.0,
                        "x1": 20.0,
                        "y1": 20.0,
                    }
                ],
                "image_boxes": [],
                "raster": {"path": "page-1.png", "width": 1224, "height": 1584},
            }
        ],
    }
    analysis_path = evidence / "output" / "analysis.json"
    atomic_write_json(analysis_path, analysis)
    toolchain = {
        key: _image_record()[key]
        for key in ("image_id", "image_digest", "lock_sha256", "platform")
    }
    manifest = {
        "schema": "amipro-oracle-job-v1",
        "result_schema": "amipro-oracle-native-document-result-v1",
        "backend": "real",
        "baseline_eligible": False,
        "status": "success",
        "source": {
            "size": source.stat().st_size,
            "sha256": source_hash,
            "staged_name": "DOC00001.SAM",
        },
        "toolchain": toolchain,
        "analysis_path": "output/analysis.json",
        "artifacts": [
            _artifact(analysis_path, evidence, "analysis"),
            _artifact(native_pdf, evidence, "derived-output"),
            _artifact(raster, evidence, "derived-output"),
        ],
    }
    manifest_path = evidence / "job.json"
    atomic_write_json(manifest_path, manifest)
    record = {
        "index": 1,
        "source": "invented.sam",
        "guest": "DOC00001.SAM",
        "preflight": "ready",
        "source_size": source.stat().st_size,
        "source_sha256": source_hash,
        "audit": {"status": "invented-safe"},
    }
    plan_identity = {
        "schema": "amipro-oracle-real-batch-plan-v1",
        "document_count": 1,
        "records": [record],
    }
    plan = {**plan_identity, "plan_digest": digest_json(plan_identity)}
    result = {
        "schema": "amipro-oracle-real-batch-document-v1",
        "backend": "real",
        "baseline_eligible": False,
        "status": "success",
        "index": 1,
        "source": "invented.sam",
        "source_size": source.stat().st_size,
        "source_sha256": source_hash,
        "guest": "DOC00001.SAM",
        "evidence_job": evidence.name,
        "job_manifest_sha256": sha256_file(manifest_path),
        "pdf": {
            "path": "reference-pdf/invented.pdf",
            "size": reference_pdf.stat().st_size,
            "sha256": sha256_file(reference_pdf),
            "page_count": 1,
        },
    }
    journal = {
        "schema": "amipro-oracle-real-batch-v1",
        "backend": "real",
        "baseline_eligible": False,
        "status": "success",
        "plan_digest": plan["plan_digest"],
        "document_count": 1,
        "success_count": 1,
        "failure_count": 0,
        "pending_count": 0,
        "jobs": [result],
    }
    atomic_write_json(batch / "plan.json", plan)
    atomic_write_json(batch / "batch.json", journal)
    atomic_write_json(batch / "jobs" / "00001" / "result.json", result)
    return home, batch, source_root, _image_record()


def test_retained_batch_selection_verifies_all_invented_identities(tmp_path: Path) -> None:
    home, batch, source_root, image = _invented_native_batch(tmp_path)

    selected = select_native_documents(
        home=home,
        batch_root=batch,
        source_root=source_root,
        image_record=image,
        expected_successes=1,
        expected_failures=0,
    )

    assert len(selected.documents) == 1
    assert selected.documents[0].analysis["page_count"] == 1
    assert selected.analysis_profile == ANALYSIS_PROFILE

    (batch / "reference-pdf" / "invented.pdf").write_bytes(b"changed invented bytes")
    with pytest.raises(OracleError, match="reference PDF identity changed"):
        select_native_documents(
            home=home,
            batch_root=batch,
            source_root=source_root,
            image_record=image,
            expected_successes=1,
            expected_failures=0,
        )


def test_native_profile_whitespace_policy_is_comparable() -> None:
    assert (
        _normalize_text(
            "INVENTED CONTENT\r\n\r\n",
            "pdftotext-page-text-trailing-newlines-trimmed",
        )
        == "INVENTED CONTENT"
    )


def test_pillow_raster_measurement_matches_bounded_stdlib_measurement(
    tmp_path: Path,
) -> None:
    expected = tmp_path / "expected.png"
    actual = tmp_path / "actual.png"
    expected.write_bytes(
        encode_rgb_png(
            3,
            2,
            bytes(
                (
                    0,
                    0,
                    0,
                    255,
                    255,
                    255,
                    10,
                    20,
                    30,
                    40,
                    50,
                    60,
                    70,
                    80,
                    90,
                    100,
                    110,
                    120,
                )
            ),
        )
    )
    actual.write_bytes(
        encode_rgb_png(
            3,
            2,
            bytes(
                (
                    0,
                    0,
                    0,
                    240,
                    255,
                    255,
                    10,
                    20,
                    31,
                    40,
                    52,
                    60,
                    70,
                    80,
                    90,
                    100,
                    110,
                    120,
                )
            ),
        )
    )

    stdlib = raster_difference(expected, actual, pixel_threshold=0.05)
    pillow = raster_difference(expected, actual, pixel_threshold=0.05, backend="pillow")

    assert pillow == stdlib


def test_differential_bbox_parser_retains_bounded_off_page_measurements(
    tmp_path: Path,
) -> None:
    bbox = tmp_path / "bbox.html"
    bbox.write_text(
        """<html xmlns="http://www.w3.org/1999/xhtml"><body><doc>
<page width="100" height="100"><flow><block><line>
<word xMin="-2" yMin="10" xMax="8" yMax="20">INVENTED</word>
</line></block></flow></page></doc></body></html>""",
        encoding="utf-8",
    )

    with pytest.raises(OracleError, match="outside its page"):
        _bbox_pages(bbox, 1)

    pages = _bbox_pages(bbox, 1, allow_bounded_off_page=True)
    assert pages[0]["text_boxes"][0] == {
        "text": "INVENTED",
        "x0": -2.0,
        "y0": 10.0,
        "x1": 8.0,
        "y1": 20.0,
    }


def test_aggregate_suppresses_rare_invented_groups_and_prioritizes_fixtures() -> None:
    results = []
    for index in range(10):
        mismatch = "page-raster" if index < 6 else "invented-rare-class"
        results.append(
            {
                "status": "compared",
                "comparison": {
                    "equal": False,
                    "mismatch_classes": [mismatch],
                    "issue_counts": {mismatch: 2},
                },
            }
        )
    failures = tuple(
        {"status": "failure", "class": "failed-timeout"} for _index in range(6)
    ) + ({"status": "blocked", "class": "invented-rare-block"},)

    aggregate = build_aggregate(results, failures)

    assert aggregate["schema"] == PRIVATE_CORPUS_AGGREGATE_SCHEMA
    groups = aggregate["mismatch_classes"]
    assert groups["reported"] == [
        {
            "class": "page-raster",
            "documents": 6,
            "issue_occurrences": 12,
            "frequency_percent": 60.0,
            "impact_scope": "page-or-layout",
        }
    ]
    assert groups["suppressed"] == {"class_count": 1, "document_occurrences": 4}
    serialized = json.dumps(aggregate, sort_keys=True)
    assert "invented-rare-class" not in serialized
    assert "invented-rare-block" not in serialized
    assert aggregate["fixture_priorities"][0]["mismatch_class"] == "page-raster"


def test_aggregate_audit_rejects_detail_fields_and_rare_groups() -> None:
    report = {
        "schema": PRIVATE_CORPUS_AGGREGATE_SCHEMA,
        "privacy": {"minimum_group_size": 5},
        "mismatch_classes": {"reported": [], "suppressed": {}},
        "path": "invented/private.sam",
    }
    with pytest.raises(OracleError, match="detail-bearing"):
        audit_aggregate(report)

    report.pop("path")
    report["mismatch_classes"]["reported"] = [
        {"class": "invented-rare", "documents": 1}
    ]
    with pytest.raises(OracleError, match="rare group"):
        audit_aggregate(report)
