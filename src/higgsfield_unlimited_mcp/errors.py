"""Custom exceptions for Higgsfield Unlimited MCP."""


class HiggsfieldError(Exception):
    """Base class for all Higgsfield MCP errors."""


class AuthError(HiggsfieldError):
    """Raised when Clerk auth fails (bad cookie, expired session, etc.)."""


class ConfigError(HiggsfieldError):
    """Raised when required configuration is missing or invalid."""


class JobSubmitError(HiggsfieldError):
    """Raised when a job cannot be submitted (bad params, server error)."""


class JobFailedError(HiggsfieldError):
    """Raised when a job reaches a failed/error terminal state."""


class JobTimeoutError(HiggsfieldError):
    """Raised when polling exceeds the timeout budget."""


class JobNotFoundError(HiggsfieldError):
    """Raised when a job_id is not in the local registry."""
