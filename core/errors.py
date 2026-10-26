"""Application-level exception hierarchy."""


class InkfallError(Exception):
    """Base class for all Inkfall errors."""


class DocumentError(InkfallError):
    """Raised for document open/close/page-access failures."""


class RenderError(InkfallError):
    """Raised for render request/processing failures."""


class AnnotationError(InkfallError):
    """Raised for annotation validation/storage failures."""


class ToolError(InkfallError):
    """Raised for unknown tools or invalid tool configuration."""
