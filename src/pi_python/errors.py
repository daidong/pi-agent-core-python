"""Stable exception classes; exceptions never imply that side effects were rolled back."""


class PiError(Exception):
    pass


class ConfigurationError(PiError):
    pass


class MessageValidationError(PiError):
    pass


class UnsupportedCapabilityError(MessageValidationError):
    pass


class ProviderProtocolError(PiError):
    pass


class AgentBusyError(PiError):
    pass


class AgentClosedError(PiError):
    pass


class InvalidContinuationError(PiError):
    pass


class ToolOutcomeUnknownError(PiError):
    pass


class CleanupTimeoutError(PiError):
    pass


class SubscriptionError(PiError):
    pass


class CandidateValidationError(PiError):
    pass
