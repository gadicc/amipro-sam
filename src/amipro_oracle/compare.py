from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .constants import ANALYSIS_SCHEMA, COMPARE_SCHEMA, JOB_SCHEMA
from .io import read_json_object, sha256_file
from .raster import raster_difference


@dataclass(frozen=True)
class _AnalysisSource:
    root: Path
    provenance: str
    baseline_eligible: bool
    verified_artifacts: frozenset[Path]


def _relative_file(root: Path, value: object) -> Path:
    if not isinstance(value, str) or not value:
        raise ValueError("artifact path must be a non-empty string")
    candidate = Path(value)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ValueError(f"artifact path must be relative: {value!r}")
    resolved_root = root.resolve()
    lexical = resolved_root / candidate
    resolved = lexical.resolve()
    if resolved_root not in resolved.parents:
        raise ValueError(f"artifact escapes its job directory: {value!r}")
    return lexical


def _finite_number(value: object, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a finite number") from exc
    if not math.isfinite(number):
        raise ValueError(f"{label} must be a finite number")
    return number


def _verify_job_artifacts(job_path: Path, manifest: dict[str, Any]) -> dict[Path, dict[str, Any]]:
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        raise ValueError(f"job manifest has no artifact inventory: {job_path}")
    verified: dict[Path, dict[str, Any]] = {}
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            raise ValueError(f"job artifact entry must be an object: {job_path}")
        path = _relative_file(job_path.parent, artifact.get("path"))
        if path in verified:
            raise ValueError(f"duplicate job artifact path: {path}")
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"job artifact must be a regular non-symlink file: {path}")
        size = artifact.get("size")
        digest = artifact.get("sha256")
        if type(size) is not int or size < 0 or path.stat().st_size != size:
            raise ValueError(f"job artifact size mismatch: {path}")
        if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise ValueError(f"job artifact has invalid SHA-256: {path}")
        if sha256_file(path) != digest:
            raise ValueError(f"job artifact hash mismatch: {path}")
        verified[path] = artifact
    return verified


def _real_job_is_baseline_eligible(manifest: dict[str, Any]) -> bool:
    # Phase 1 has no real runtime-verification path. A self-consistent JSON object
    # is not an attestation, so fail closed until the real job producer verifies
    # the media, runtime cache, OCI image, process, and capture identities.
    return False


def load_analysis(path: Path) -> tuple[dict[str, Any], _AnalysisSource]:
    candidate = path
    if candidate.is_dir():
        candidate = candidate / "job.json"
    value = read_json_object(candidate)
    if value.get("schema") == JOB_SCHEMA:
        verified = _verify_job_artifacts(candidate, value)
        analysis_path = _relative_file(candidate.parent, value.get("analysis_path"))
        analysis_artifact = verified.get(analysis_path)
        if analysis_artifact is None or analysis_artifact.get("kind") not in {
            "analysis",
            # Retained native-document v1/v2 jobs classified every file below
            # output/ as derived output.  The manifest's analysis_path and the
            # analysis schema still identify this artifact unambiguously.
            "derived-output",
        }:
            raise ValueError(
                f"job analysis is absent from its verified artifact inventory: {candidate}"
            )
        analysis = read_json_object(analysis_path)
        if analysis.get("backend") != value.get("backend"):
            raise ValueError(f"job and analysis backend disagree: {candidate}")
        source = _AnalysisSource(
            root=analysis_path.parent,
            provenance="verified-job",
            baseline_eligible=_real_job_is_baseline_eligible(value),
            verified_artifacts=frozenset(verified),
        )
    else:
        analysis = value
        source = _AnalysisSource(
            root=candidate.parent,
            provenance="raw-analysis",
            baseline_eligible=False,
            verified_artifacts=frozenset(),
        )
    if analysis.get("schema") != ANALYSIS_SCHEMA:
        raise ValueError(f"unsupported analysis schema in {candidate}")
    return analysis, source


def _normalize_text(value: object, policy: str) -> str:
    if not isinstance(value, str):
        raise ValueError("analysis text must be a string")
    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    if policy == "exact":
        return normalized
    if policy == "pdftotext-page-text-trailing-newlines-trimmed":
        return normalized.rstrip("\n")
    if policy == "collapse":
        return re.sub(r"\s+", " ", normalized).strip()
    raise ValueError(f"unsupported whitespace policy: {policy}")


def _box_issues(
    expected: object,
    actual: object,
    *,
    tolerance: float,
    page_number: int,
    kind: str,
    whitespace: str,
) -> list[dict[str, object]]:
    if not isinstance(expected, list) or not isinstance(actual, list):
        raise ValueError(f"{kind} boxes must be lists")
    issues: list[dict[str, object]] = []
    if len(expected) != len(actual):
        issues.append(
            {
                "code": f"{kind}-box-count",
                "page": page_number,
                "expected": len(expected),
                "actual": len(actual),
            }
        )
    for index, (expected_box, actual_box) in enumerate(zip(expected, actual, strict=False)):
        if not isinstance(expected_box, dict) or not isinstance(actual_box, dict):
            raise ValueError(f"{kind} box entries must be objects")
        for coordinate in ("x0", "y0", "x1", "y1"):
            expected_value = _finite_number(
                expected_box.get(coordinate), f"expected {kind} box {coordinate}"
            )
            actual_value = _finite_number(
                actual_box.get(coordinate), f"actual {kind} box {coordinate}"
            )
            if abs(expected_value - actual_value) > tolerance:
                issues.append(
                    {
                        "code": f"{kind}-box-position",
                        "page": page_number,
                        "index": index,
                        "coordinate": coordinate,
                        "expected": expected_value,
                        "actual": actual_value,
                        "tolerance": tolerance,
                    }
                )
        if kind == "text" and _normalize_text(
            expected_box.get("text", ""), whitespace
        ) != _normalize_text(actual_box.get("text", ""), whitespace):
            issues.append({"code": "text-box-content", "page": page_number, "index": index})
    return issues


def compare_analyses(
    expected_path: Path,
    actual_path: Path,
    *,
    bbox_tolerance: float = 0.5,
    raster_rmse: float = 0.01,
    pixel_threshold: float = 0.05,
    max_different_ratio: float = 0.001,
    raster_backend: str = "stdlib",
) -> dict[str, object]:
    tolerances = {
        "bbox tolerance": _finite_number(bbox_tolerance, "bbox tolerance"),
        "raster RMSE": _finite_number(raster_rmse, "raster RMSE"),
        "pixel threshold": _finite_number(pixel_threshold, "pixel threshold"),
        "maximum different-pixel ratio": _finite_number(
            max_different_ratio, "maximum different-pixel ratio"
        ),
    }
    if min(tolerances.values()) < 0:
        raise ValueError("comparison tolerances must be non-negative")
    if raster_rmse > 1 or pixel_threshold > 1 or max_different_ratio > 1:
        raise ValueError("normalized raster tolerances must not exceed one")
    expected, expected_source = load_analysis(expected_path)
    actual, actual_source = load_analysis(actual_path)
    issues: list[dict[str, object]] = []
    expected_backend = expected.get("backend")
    actual_backend = actual.get("backend")
    if expected_backend not in {"real", "fake"} or actual_backend not in {"real", "fake"}:
        raise ValueError("analysis backend must be real or fake")
    if expected_backend != actual_backend:
        issues.append(
            {
                "code": "backend-mismatch",
                "expected": expected_backend,
                "actual": actual_backend,
            }
        )
    expected_profile = expected.get("profile")
    actual_profile = actual.get("profile")
    if not isinstance(expected_profile, dict) or not isinstance(actual_profile, dict):
        raise ValueError("analysis profile must be an object")
    profile_equal = expected_profile == actual_profile
    if not profile_equal:
        issues.append(
            {
                "code": "profile-mismatch",
                "expected": expected.get("profile"),
                "actual": actual.get("profile"),
            }
        )
    whitespace = str(expected_profile.get("whitespace", "exact"))
    expected_pages = expected.get("pages")
    actual_pages = actual.get("pages")
    if not isinstance(expected_pages, list) or not isinstance(actual_pages, list):
        raise ValueError("analysis pages must be lists")
    if expected.get("page_count") != len(expected_pages):
        raise ValueError("expected analysis page_count disagrees with pages")
    if actual.get("page_count") != len(actual_pages):
        raise ValueError("actual analysis page_count disagrees with pages")
    if len(expected_pages) != len(actual_pages):
        issues.append(
            {"code": "page-count", "expected": len(expected_pages), "actual": len(actual_pages)}
        )

    raster_reports: list[dict[str, object]] = []
    for page_index, (expected_page, actual_page) in enumerate(
        zip(expected_pages, actual_pages, strict=False), start=1
    ):
        if not isinstance(expected_page, dict) or not isinstance(actual_page, dict):
            raise ValueError("analysis page entries must be objects")
        page_number = int(expected_page.get("number", page_index))
        for geometry in ("width_pt", "height_pt"):
            expected_value = _finite_number(
                expected_page.get(geometry), f"expected page {page_number} {geometry}"
            )
            actual_value = _finite_number(
                actual_page.get(geometry), f"actual page {page_number} {geometry}"
            )
            if abs(expected_value - actual_value) > bbox_tolerance:
                issues.append(
                    {
                        "code": "page-geometry",
                        "page": page_number,
                        "coordinate": geometry,
                        "expected": expected_value,
                        "actual": actual_value,
                    }
                )
        if _normalize_text(expected_page.get("text", ""), whitespace) != _normalize_text(
            actual_page.get("text", ""), whitespace
        ):
            issues.append({"code": "page-text", "page": page_number})
        issues.extend(
            _box_issues(
                expected_page.get("text_boxes", []),
                actual_page.get("text_boxes", []),
                tolerance=bbox_tolerance,
                page_number=page_number,
                kind="text",
                whitespace=whitespace,
            )
        )
        issues.extend(
            _box_issues(
                expected_page.get("image_boxes", []),
                actual_page.get("image_boxes", []),
                tolerance=bbox_tolerance,
                page_number=page_number,
                kind="image",
                whitespace=whitespace,
            )
        )

        expected_raster = expected_page.get("raster")
        actual_raster = actual_page.get("raster")
        if isinstance(expected_raster, dict) and isinstance(actual_raster, dict):
            expected_raster_path = _relative_file(
                expected_source.root, expected_raster.get("path")
            )
            actual_raster_path = _relative_file(actual_source.root, actual_raster.get("path"))
            if (
                expected_source.provenance == "verified-job"
                and expected_raster_path not in expected_source.verified_artifacts
            ):
                raise ValueError("expected raster is absent from its job artifact inventory")
            if (
                actual_source.provenance == "verified-job"
                and actual_raster_path not in actual_source.verified_artifacts
            ):
                raise ValueError("actual raster is absent from its job artifact inventory")
            report = raster_difference(
                expected_raster_path,
                actual_raster_path,
                pixel_threshold=pixel_threshold,
                backend=raster_backend,
            )
            report["page"] = page_number
            raster_reports.append(report)
            if (
                not report["dimensions_equal"]
                or report["rmse"] is None
                or float(report["rmse"]) > raster_rmse
                or float(report["different_pixel_ratio"]) > max_different_ratio
            ):
                issues.append({"code": "page-raster", "page": page_number, **report})
        elif expected_raster is not None or actual_raster is not None:
            issues.append({"code": "missing-page-raster", "page": page_number})

    return {
        "schema": COMPARE_SCHEMA,
        "equal": not issues,
        "baseline_eligible": (
            expected_source.baseline_eligible and actual_source.baseline_eligible
        ),
        "expected_provenance": expected_source.provenance,
        "actual_provenance": actual_source.provenance,
        "expected_backend": expected_backend,
        "actual_backend": actual_backend,
        "thresholds": {
            "bbox_points": bbox_tolerance,
            "raster_rmse": raster_rmse,
            "pixel_threshold": pixel_threshold,
            "max_different_pixel_ratio": max_different_ratio,
            "raster_backend": raster_backend,
        },
        "issues": issues,
        "rasters": raster_reports,
    }
