"""Small shared scanners for the mixed text/binary SAM container grammar."""

from __future__ import annotations

import re
from dataclasses import dataclass

# A non-letter boundary recognizes malformed metadata conservatively while
# keeping unrelated commands such as ``<:FootLike>`` out of the structural
# grammar.  Malformed structural records must still consume their own close so
# they cannot truncate the outer EDOC stream.
MULTILINE_CONTAINER = re.compile(r"(?<!<)<:(?P<kind>[NFHh])(?=$|[^A-Za-z])")
_MULTILINE_PREFIX = re.compile(r"(?<!<)<:(?P<kind>[NFHh])")


@dataclass(frozen=True, slots=True)
class ContainerScan:
    opener: re.Match[str] | None
    standalone_terminator: bool


class MultilineContainerScanner:
    """Stateful scanner shared by text parsing and binary-tail discovery."""

    def __init__(self) -> None:
        self._inline_open = False
        self._inline_in_quote = False

    def scan_line(self, line: str) -> ContainerScan:
        index = 0
        continued_inline = self._inline_open
        if continued_inline:
            end, self._inline_in_quote = _inline_content_end_state(
                line,
                0,
                quoted=True,
                in_quote=self._inline_in_quote,
            )
            if end is None:
                return ContainerScan(None, False)
            self._inline_open = False
            self._inline_in_quote = False
            index = end + 1

        while index < len(line):
            if line.startswith("<<", index):
                index += 2
                continue
            if line[index] != "<":
                index += 1
                continue

            match = MULTILINE_CONTAINER.match(line, index)
            if match is not None:
                return ContainerScan(match, False)

            dynamic = line.startswith(("<:X", "<:Z"), index)
            end, quote_state = _inline_content_end_state(
                line,
                index + 1,
                quoted=dynamic,
            )
            # A letter-prefixed form such as ``<:Fbad`` is ambiguous only
            # while open. Treating that unclosed form as a malformed record
            # prevents its close from truncating the outer EDOC stream, while
            # a closed ``<:FootLike>`` remains an ordinary inline command.
            if end is None and (match := _MULTILINE_PREFIX.match(line, index)):
                return ContainerScan(match, False)
            if end is not None:
                index = end + 1
                continue
            if dynamic:
                # Inline fields may span physical lines; remember that state
                # so container-looking fallback text on following lines stays
                # inside the field in both the parser and decoding window.
                self._inline_open = True
                self._inline_in_quote = quote_state
                return ContainerScan(None, False)
            # A literal/corrupt angle is not enough evidence to hide a later
            # structural opener on the same physical line.
            index += 1

        return ContainerScan(
            None,
            line.strip() == ">" and not continued_inline,
        )


def multiline_container_openers(line: str) -> int:
    """Count evidenced multiline-container openers in one physical line."""

    # The rest of an opener line is metadata, so an apparent second token on
    # that physical line is not a second structural child.
    return int(find_multiline_container(line) is not None)


def find_multiline_container(line: str) -> re.Match[str] | None:
    """Find an opener without mistaking text inside another inline command."""

    return MultilineContainerScanner().scan_line(line).opener


def _inline_command_end(line: str, start: int, *, quoted: bool = False) -> int | None:
    """Find an inline command close without stopping on escaped ``>`` bytes."""

    return _inline_content_end(line, start + 1, quoted=quoted)


def _inline_content_end(line: str, start: int, *, quoted: bool = False) -> int | None:
    return _inline_content_end_state(line, start, quoted=quoted)[0]


def _inline_content_end_state(
    line: str,
    start: int,
    *,
    quoted: bool,
    in_quote: bool = False,
) -> tuple[int | None, bool]:
    index = start
    while index < len(line):
        if line.startswith(("<;>", "<[>"), index):
            index += 3
            continue
        if (
            index + 3 < len(line)
            and line[index] == "<"
            and line[index + 1] in {"/", "\\"}
            and line[index + 3] == ">"
        ):
            index += 4
            continue
        if quoted and line[index] == '"':
            in_quote = not in_quote
            index += 1
            continue
        if line[index] == ">" and not in_quote:
            return index, in_quote
        index += 1
    return None, in_quote


def is_standalone_terminator(line: str) -> bool:
    """Return whether *line* contains only the SAM ``>`` terminator."""

    return line.strip() == ">"
