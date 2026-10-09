"""Errors the pipeline raises on purpose. The CLI turns these into a message and a non-zero exit code;
anything else is a bug and keeps its traceback."""
from __future__ import annotations


class PipelineError(Exception):
    """Base class for expected failures."""


class InputError(PipelineError):
    """The input file cannot be processed."""


class CorruptPdfError(InputError):
    """The file is not a readable PDF."""


class EncryptedPdfError(InputError):
    """The PDF is password protected and cannot be opened without the password."""


class EmptyPdfError(InputError):
    """The PDF has no pages."""
