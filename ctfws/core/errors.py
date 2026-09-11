"""Application-specific exceptions."""


class CTFWSError(Exception):
    """Base exception that can be rendered as a user-facing CLI error."""


class WorkspaceNotFoundError(CTFWSError):
    """Raised when a workspace database cannot be found."""


class DuplicateEntityError(CTFWSError):
    """Raised when an entity would violate a workspace uniqueness rule."""


class EntityNotFoundError(CTFWSError):
    """Raised when a requested entity does not exist."""


class IdempotencyConflictError(CTFWSError):
    """Raised when one idempotency key is reused with different content."""


class InvalidWorkspaceError(CTFWSError):
    """Raised when a path is not a valid CTF Workspace directory."""
