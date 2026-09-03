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


class TurnBusy(DainoError):
    """Raised when an agentic turn is asked for while another one is running.

    One working tree and one runtime cannot serve two agents at once. Callers
    that can wait take the turn lock; callers that cannot — an HTTP request
    behind a button — get this instead, so the user is told rather than left
    watching a request that will time out.
    """


class VerificationError(DainoError):
    """Raised when required verification does not pass."""


class WorkspaceError(DainoError):
    """Raised when an isolated workspace cannot be safely created."""


class DeploymentError(DainoError):
    """Raised for failed or unsafe deployments."""


#: Backwards-compatible alias for the pre-rename base exception name.
VasukiError = DainoError
