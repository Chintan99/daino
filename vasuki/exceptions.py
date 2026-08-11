"""Domain exceptions with safe, user-facing messages."""


class VasukiError(Exception):
    """Base exception for expected Vasuki failures."""


class ConfigurationError(VasukiError):
    """Raised when project or provider configuration is invalid."""


class PolicyDenied(VasukiError):
    """Raised when an operation is denied by security policy."""


class ProviderError(VasukiError):
    """Raised when a model provider fails."""


class ToolCallingUnsupported(ProviderError):
    """Raised when a backend rejects an otherwise valid native-tools request."""


class StructuredConstraintUnsupported(ProviderError):
    """Raised when a backend rejects its JSON-schema decoding parameter."""


class VerificationError(VasukiError):
    """Raised when required verification does not pass."""


class WorkspaceError(VasukiError):
    """Raised when an isolated workspace cannot be safely created."""


class DeploymentError(VasukiError):
    """Raised for failed or unsafe deployments."""
