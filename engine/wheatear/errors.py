"""Typed error hierarchy for Wheatear.

Every failure a user can hit is one of these, so the CLI (and any other
frontend) can present a clear, actionable message instead of a raw traceback.
Internal bugs stay as plain exceptions -- these are only for conditions we
expect and have something useful to say about.

    WheatearError
    ├── UnrecognizedExportError   -- input isn't a shape we know how to read
    ├── UnsupportedCorridorError  -- no path from this source to this target
    ├── ImportError_              -- source export was recognized but unreadable
    ├── MapError                  -- reference resolution failed
    ├── TranslateError            -- the (optional) LLM stage failed
    └── ExportError               -- writing the target artifact failed
"""

from __future__ import annotations


class WheatearError(Exception):
    """Base class for every expected, user-facing Wheatear failure."""


class UnrecognizedExportError(WheatearError):
    """The input path doesn't match any importer's known export shape."""


class UnsupportedCorridorError(WheatearError):
    """The requested (source, target) platform pair isn't supported."""


class ImportError_(WheatearError):
    """A recognized export could not be parsed (malformed/incomplete)."""


class MapError(WheatearError):
    """The Map stage could not resolve references into target tools/knowledge."""


class TranslateError(WheatearError):
    """The Translate (LLM) stage failed."""


class ExportError(WheatearError):
    """The target artifact could not be written."""
