"""Public exception hierarchy."""


class AmiProError(Exception):
    """Base exception for expected converter failures."""


class DecodeError(AmiProError):
    """The input byte stream could not be decoded safely."""


class ParseError(AmiProError):
    """The input is not a supported or recoverable SAM document."""


class ResourceLimitError(AmiProError):
    """A configured parser or renderer safety limit was exceeded."""


class RenderError(AmiProError):
    """A parsed document could not be rendered in the requested format."""
