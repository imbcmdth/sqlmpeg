"""Error types for sqlmpeg.

Every rejection produced by the library is a `SqlmpegError` carrying a typed
`ErrorCode`, a human message, optional line/col anchoring, and an optional
hint. See the "Error contract" section of sqlmpeg-project.md.
"""

from __future__ import annotations

from enum import Enum


class ErrorCode(str, Enum):
    PARSE_ERROR = "PARSE_ERROR"
    UNKNOWN_FUNCTION = "UNKNOWN_FUNCTION"
    UNKNOWN_ALIAS = "UNKNOWN_ALIAS"
    UDF_ARG_TYPE = "UDF_ARG_TYPE"
    SINGLE_OUTPUT_ONLY = "SINGLE_OUTPUT_ONLY"  # retired in v2: multi-column SELECT
    # is legal now (RFC-001); kept in the enum for the docs' sake, not raised
    # by any v2 code path.
    NO_STREAMING_EQUIVALENT = "NO_STREAMING_EQUIVALENT"
    CONCAT_MISMATCH = "CONCAT_MISMATCH"  # reserved for v1 probing
    UNSUPPORTED_SQL = "UNSUPPORTED_SQL"  # catch-all for constructs outside dialect
    STREAM_NOT_FOUND = "STREAM_NOT_FOUND"  # probed subscript out of range
    INPUT_NOT_FOUND = "INPUT_NOT_FOUND"  # */splat/broadcast needs a readable
    # input and there is none
    BROADCAST_MISMATCH = "BROADCAST_MISMATCH"  # zip length mismatch across
    # broadcast-expanded array arguments
    UNKNOWN_SINK_OPTION = "UNKNOWN_SINK_OPTION"  # COPY ... WITH (...) names an
    # option not in sqlmpeg.sink.SINK_OPTIONS
    SINK_OPTION_TYPE = "SINK_OPTION_TYPE"  # COPY ... WITH (...) option value
    # doesn't match its declared type (str/int/bool)
    UNKNOWN_FILTER_OPTION = "UNKNOWN_FILTER_OPTION"  # a `name => value` argument
    # names an option the ffmpeg filter it targets does not have (RFC-003)
    FILTER_OPTION_TYPE = "FILTER_OPTION_TYPE"  # a `name => value` argument's value
    # does not match the option's introspected type, range or constant set
    UNKNOWN_INPUT_OPTION = "UNKNOWN_INPUT_OPTION"  # input('path', name => value)
    # names an option not in sqlmpeg.inputs.INPUT_OPTIONS
    INPUT_OPTION_TYPE = "INPUT_OPTION_TYPE"  # input('path', name => value)'s
    # value doesn't match its declared type (str/int/bool/num)
    INTERNAL = "INTERNAL"  # bug backstop; fuzz asserts this never fires


class SqlmpegError(Exception):
    """Structured, machine-readable error for every user-input rejection.

    Library code that consumes user input must never raise anything other
    than this.
    """

    code: ErrorCode
    message: str
    line: int | None
    col: int | None
    hint: str | None

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        line: int | None = None,
        col: int | None = None,
        hint: str | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.line = line
        self.col = col
        self.hint = hint
        super().__init__(str(self))

    def to_dict(self) -> dict[str, object]:
        return {
            "line": self.line,
            "col": self.col,
            "code": self.code.value,
            "message": self.message,
            "hint": self.hint,
        }

    def __str__(self) -> str:
        parts: list[str] = []
        if self.line is not None and self.col is not None:
            parts.append(f"line {self.line}:{self.col}: ")
        elif self.line is not None:
            parts.append(f"line {self.line}: ")
        parts.append(f"{self.code.value}: {self.message}")
        if self.hint is not None:
            parts.append(f" (hint: {self.hint})")
        return "".join(parts)
