"""What can go wrong between the application and a model. None of these carries a prompt, an
answer or a key: their text may end up in logs and in the audit log."""


class LLMError(Exception):
    """Base of every error of the gateway. `code` is a fixed word, safe to log and to show."""

    code = "llm_error"

    def __init__(self, message: str = "") -> None:
        super().__init__(message or self.code)


class LLMDisabled(LLMError):
    code = "llm_disabled"  # no provider is configured: the product works without a model


class LLMNotConfigured(LLMError):
    code = "llm_not_configured"  # a provider, but no model for this stage


class NotPublicArticle(LLMError):
    code = "not_public_article"  # the data class rule of ADR-0015: only class A may go out


class BudgetExhausted(LLMError):
    code = "budget_exhausted"  # the day's cap would be passed; nothing was sent


class BudgetUnavailable(LLMError):
    code = "budget_unavailable"  # the counter could not be read or written: fail closed


class CallRateExceeded(LLMError):
    code = "call_rate_exceeded"  # too many calls this minute


class StagePaused(LLMError):
    code = "stage_paused"  # the stage kept failing and is resting for a while


class ProviderFailed(LLMError):
    """The provider did not answer in time or with an error.

    `sent` says whether the request may have reached the provider and been billed (a timeout, a
    connection that broke): then the booking is kept and counted, not given back. A provider that
    answered with an error status, or a request that never left, was not billed (`sent` False).
    """

    code = "provider_failed"

    def __init__(self, message: str = "", *, sent: bool = True) -> None:
        super().__init__(message)
        self.sent = sent


class ProviderRejected(LLMError):
    code = "provider_rejected"  # a wrong key or a refused request: retrying cannot help


class SchemaRejected(LLMError):
    code = "schema_rejected"  # the answer is not JSON of the expected shape; it is never repaired
