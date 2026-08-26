"""Domain exceptions with safe, user-facing messages."""


class DainoError(Exception):
    """Base exception for expected Daino failures."""


class ConfigurationError(DainoError):
    """Raised when project or provider configuration is invalid."""


class PolicyDenied(DainoError):
    """Raised when an operation is denied by security policy."""


class ProviderError(DainoError):
    """Raised when a model provider fails."""


class ToolCallingUnsupported(ProviderError):
    """Raised when a backend rejects an otherwise valid native-tools request."""


class StructuredConstraintUnsupported(ProviderError):
    """Raised when a backend rejects its JSON-schema decoding parameter."""


class VerificationError(DainoError):
    """Raised when required verification does not pass."""


class WorkspaceError(DainoError):
    """Raised when an isolated workspace cannot be safely created."""


class DeploymentError(DainoError):
    """Raised for failed or unsafe deployments."""


#: Backwards-compatible alias for the pre-rename base exception name.
VasukiError = DainoError
