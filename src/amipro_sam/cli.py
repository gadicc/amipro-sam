"""Command-line interface for conversion, inspection, and IR dumps."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from collections import Counter
from collections.abc import Callable, Sequence
from contextlib import suppress
from pathlib import Path

from .errors import AmiProError, RenderError
from .parser import parse_file

_FORMAT_ALIASES = {
    "html": "html",
    "md": "markdown",
    "markdown": "markdown",
    "txt": "text",
    "text": "text",
    "json": "json",
    "odt": "odt",
    "pdf": "pdf",
    "docx": "docx",
}
_EXTENSIONS = {
    "html": ".html",
    "markdown": ".md",
    "text": ".txt",
    "json": ".json",
    "odt": ".odt",
    "pdf": ".pdf",
    "docx": ".docx",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="amipro-sam",
        description="Recover and convert Lotus Ami Pro 3.x SAM documents.",
    )
    parser.add_argument("--version", action="version", version="%(prog)s 0.1.0")
    subparsers = parser.add_subparsers(dest="command", required=True)

    convert = subparsers.add_parser("convert", help="convert one file or a batch")
    convert.add_argument("inputs", nargs="+", type=Path, help="SAM file(s) or directories")
    convert.add_argument(
        "--format",
        "-f",
        choices=sorted(_FORMAT_ALIASES),
        help="output format; inferred from --output for one file when omitted",
    )
    convert.add_argument("--output", "-o", type=Path, help="output file or batch directory")
    convert.add_argument(
        "--force", action="store_true", help="replace an existing output (never a source input)"
    )
    convert.add_argument("--recursive", action="store_true", help="recurse into input directories")
    convert.add_argument("--encoding", help="override source encoding")
    convert.add_argument(
        "--strict",
        action="store_true",
        help="fail on explicitly classified semantic or content preservation loss",
    )
    convert.add_argument(
        "--no-warning-summary",
        action="store_true",
        help="omit the diagnostic appendix from HTML output",
    )
    convert.set_defaults(handler=_command_convert)

    inspect = subparsers.add_parser("inspect", help="inventory SAM structures without converting")
    inspect.add_argument("inputs", nargs="+", type=Path)
    inspect.add_argument("--recursive", action="store_true")
    inspect.add_argument("--summary", action="store_true", help="show aggregate inventory")
    inspect.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    inspect.add_argument("--encoding", help="override source encoding")
    inspect.set_defaults(handler=_command_inspect)

    dump = subparsers.add_parser("dump", help="write the parsed intermediate representation")
    dump.add_argument("input", type=Path)
    dump.add_argument("--format", choices=("json",), default="json")
    dump.add_argument("--output", "-o", type=Path)
    dump.add_argument(
        "--force", action="store_true", help="replace an existing output (never the source input)"
    )
    dump.add_argument("--encoding", help="override source encoding")
    dump.set_defaults(handler=_command_dump)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except (AmiProError, OSError) as exc:
        print(f"amipro-sam: error: {exc}", file=sys.stderr)
        return 1


def _command_convert(args: argparse.Namespace) -> int:
    sources = _discover(args.inputs, recursive=args.recursive)
    if not sources:
        raise RenderError("no .sam files found")
    output_format = _resolve_format(args.format, args.output, len(sources))
    renderer = _load_renderer(output_format)
    if len(sources) > 1 and args.output is None:
        raise RenderError("batch conversion requires --output DIRECTORY")
    if len(sources) > 1 and args.output.exists() and not args.output.is_dir():
        raise RenderError("batch --output must be a directory")

    destinations = {
        source: _conversion_destination(
            source, args.output, output_format, batch=len(sources) > 1
        )
        for source in sources
    }
    _preflight_destinations(
        destinations,
        sources,
        force=bool(args.force),
    )

    failures = 0
    for source in sources:
        try:
            document = parse_file(source, encoding=args.encoding, strict=args.strict)
            if output_format == "html":
                payload = renderer(
                    document, include_warnings=not bool(args.no_warning_summary)
                )
            else:
                payload = renderer(document)
            destination = destinations[source]
            if destination is None:
                sys.stdout.buffer.write(payload)
                if output_format in {"html", "markdown", "text", "json"} and not payload.endswith(
                    b"\n"
                ):
                    sys.stdout.buffer.write(b"\n")
            else:
                _atomic_write(destination, payload)
                if len(sources) > 1:
                    print(f"converted {source} -> {destination}", file=sys.stderr)
        except (AmiProError, OSError, ValueError) as exc:
            failures += 1
            print(f"{source}: {exc}", file=sys.stderr)
    return 1 if failures else 0


def _command_inspect(args: argparse.Namespace) -> int:
    sources = _discover(args.inputs, recursive=args.recursive)
    records: list[dict[str, object]] = []
    failures = 0
    for source in sources:
        try:
            document = parse_file(source, encoding=args.encoding)
            block_counts = Counter(type(block).__name__ for block in document.blocks)
            section_counts = Counter(section.name.lower() for section in document.sections)
            diagnostic_counts = Counter(item.code for item in document.diagnostics)
            severity_counts = Counter(
                getattr(item.severity, "value", str(item.severity))
                for item in document.diagnostics
            )
            loss_counts = Counter(
                item.lossiness.value
                for item in document.preservation_losses
            )
            records.append(
                {
                    "path": str(source),
                    "status": "ok",
                    "bytes": document.original_size,
                    "version": document.version,
                    "encoding": document.encoding,
                    "text_characters": len(document.text),
                    "styles": len(document.styles),
                    "blocks": dict(sorted(block_counts.items())),
                    "sections": dict(sorted(section_counts.items())),
                    "diagnostics": dict(sorted(diagnostic_counts.items())),
                    "severities": dict(sorted(severity_counts.items())),
                    "lossy": document.is_lossy,
                    "losses": dict(sorted(loss_counts.items())),
                    "unknown_records": len(document.unknown_records),
                }
            )
        except (AmiProError, OSError, ValueError) as exc:
            failures += 1
            records.append({"path": str(source), "status": "error", "error": str(exc)})

    result: object = _summarize(records) if args.summary else records
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        _print_inspection(result, summary=args.summary)
    return 1 if failures else 0


def _command_dump(args: argparse.Namespace) -> int:
    if args.output:
        _preflight_destinations(
            {args.input: args.output},
            [args.input],
            force=bool(args.force),
        )
    document = parse_file(args.input, encoding=args.encoding)
    payload = _load_renderer("json")(document)
    if args.output:
        _atomic_write(args.output, payload)
    else:
        sys.stdout.buffer.write(payload)
    return 0


def _discover(inputs: Sequence[Path], *, recursive: bool) -> list[Path]:
    result: list[Path] = []
    for item in inputs:
        if item.is_file():
            result.append(item)
        elif item.is_dir():
            iterator = item.rglob("*") if recursive else item.glob("*")
            result.extend(
                path
                for path in iterator
                if path.is_file() and path.suffix.lower() == ".sam"
            )
        else:
            raise OSError(f"input does not exist: {item}")
    return sorted(dict.fromkeys(result), key=lambda path: str(path).casefold())


def _resolve_format(requested: str | None, output: Path | None, count: int) -> str:
    if requested:
        return _FORMAT_ALIASES[requested]
    if output and count == 1:
        suffix = output.suffix.lower().lstrip(".")
        if suffix in _FORMAT_ALIASES:
            return _FORMAT_ALIASES[suffix]
    raise RenderError("--format is required unless one output filename has a recognized extension")


def _load_renderer(name: str) -> Callable[..., bytes]:
    from .renderers import get_renderer

    return get_renderer(name)


def _conversion_destination(
    source: Path, output: Path | None, output_format: str, *, batch: bool
) -> Path | None:
    if output is None:
        return None
    if batch or output.is_dir() or (not output.exists() and output.suffix == ""):
        return output / (source.stem + _EXTENSIONS[output_format])
    return output


def _preflight_destinations(
    destinations: dict[Path, Path | None],
    sources: Sequence[Path],
    *,
    force: bool,
) -> None:
    """Reject source aliases, collisions, and accidental clobbers before writing."""

    source_paths = {_normalized_path(source): source for source in sources}
    output_paths: dict[Path, Path] = {}
    for source, destination in destinations.items():
        if destination is None:
            continue
        normalized = _normalized_path(destination)
        if normalized in source_paths or _same_existing_file(destination, source):
            raise RenderError(f"refusing to replace source input: {destination}")
        previous = output_paths.get(normalized)
        if previous is not None:
            raise RenderError(
                f"multiple inputs map to the same output: {previous} and {source} -> {destination}"
            )
        output_paths[normalized] = source
        if destination.exists() and not force:
            raise RenderError(
                f"output already exists: {destination} (use --force to replace it)"
            )


def _normalized_path(path: Path) -> Path:
    return path.resolve(strict=False)


def _same_existing_file(first: Path, second: Path) -> bool:
    try:
        return first.exists() and second.exists() and os.path.samefile(first, second)
    except OSError:
        return False


def _atomic_write(destination: Path, payload: bytes) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, destination)
    except BaseException:
        with suppress(FileNotFoundError):
            os.unlink(temporary_name)
        raise


def _summarize(records: list[dict[str, object]]) -> dict[str, object]:
    blocks: Counter[str] = Counter()
    sections: Counter[str] = Counter()
    diagnostics: Counter[str] = Counter()
    severities: Counter[str] = Counter()
    losses: Counter[str] = Counter()
    total_bytes = 0
    text_characters = 0
    successful = 0
    lossy_files = 0
    for record in records:
        if record["status"] != "ok":
            continue
        successful += 1
        total_bytes += int(record["bytes"])
        text_characters += int(record["text_characters"])
        blocks.update(record["blocks"])
        sections.update(record["sections"])
        diagnostics.update(record["diagnostics"])
        severities.update(record["severities"])
        losses.update(record["losses"])
        lossy_files += int(bool(record["lossy"]))
    return {
        "files": len(records),
        "successful": successful,
        "failed": len(records) - successful,
        "bytes": total_bytes,
        "text_characters": text_characters,
        "blocks": dict(sorted(blocks.items())),
        "sections": dict(sorted(sections.items())),
        "diagnostics": dict(sorted(diagnostics.items())),
        "severities": dict(sorted(severities.items())),
        "lossy_files": lossy_files,
        "losses": dict(sorted(losses.items())),
        "failures": [record for record in records if record["status"] != "ok"],
    }


def _print_inspection(result: object, *, summary: bool) -> None:
    if summary:
        assert isinstance(result, dict)
        print(
            f"files={result['files']} ok={result['successful']} failed={result['failed']} "
            f"lossy={result['lossy_files']} bytes={result['bytes']} "
            f"text_characters={result['text_characters']}"
        )
        for name in ("blocks", "sections", "diagnostics", "severities", "losses"):
            values = result[name]
            print(f"{name}: " + ", ".join(f"{key}={value}" for key, value in values.items()))
        for failure in result["failures"]:
            print(f"error: {failure['path']}: {failure['error']}")
        return
    assert isinstance(result, list)
    for record in result:
        if record["status"] == "error":
            print(f"{record['path']}: ERROR {record['error']}")
            continue
        print(
            f"{record['path']}: SAM v{record['version']} {record['encoding']}, "
            f"{record['text_characters']} text characters, {record['styles']} styles, "
            f"lossy={str(record['lossy']).lower()}"
        )
        print("  blocks: " + ", ".join(f"{k}={v}" for k, v in record["blocks"].items()))
        print("  sections: " + ", ".join(f"{k}={v}" for k, v in record["sections"].items()))
        if record["diagnostics"]:
            print(
                "  diagnostics: "
                + ", ".join(f"{k}={v}" for k, v in record["diagnostics"].items())
            )
        if record["losses"]:
            print(
                "  losses: "
                + ", ".join(f"{k}={v}" for k, v in record["losses"].items())
            )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
