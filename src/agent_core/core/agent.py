import logging
from typing import TYPE_CHECKING

from agent_core.characters import CharacterDefinition
from agent_core.composition import PromptContextComposer
from agent_core.core.message import Message
from agent_core.providers import (
    LLMProvider,
    LLMProviderError,
    ProviderAuthenticationError,
    ProviderConfigurationError,
    ProviderConnectionError,
    ProviderQuotaError,
    ProviderRateLimitError,
    ProviderRequestError,
    ProviderResponseError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from agent_core.temporal import (
    Clock,
    TemporalContext,
)

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from agent_core.core.business_flow import BusinessFlow

_PROVIDER_ERROR_PROTOCOL_MAP: tuple[
    tuple[
        type[LLMProviderError],
        str,
        str,
    ],
    ...,
] = (
    (
        ProviderConfigurationError,
        "PROVIDER_NOT_CONFIGURED",
        "AI provider is not configured.",
    ),
    (
        ProviderAuthenticationError,
        "PROVIDER_AUTHENTICATION_FAILED",
        "AI provider authentication failed.",
    ),
    (
        ProviderQuotaError,
        "PROVIDER_QUOTA_EXHAUSTED",
        "AI provider quota or balance is insufficient.",
    ),
    (
        ProviderRateLimitError,
        "PROVIDER_RATE_LIMITED",
        "AI provider is rate-limited. Please try again later.",
    ),
    (
        ProviderTimeoutError,
        "PROVIDER_TIMEOUT",
        "AI provider request timed out.",
    ),
    (
        ProviderConnectionError,
        "PROVIDER_UNAVAILABLE",
        "AI provider is temporarily unavailable.",
    ),
    (
        ProviderUnavailableError,
        "PROVIDER_UNAVAILABLE",
        "AI provider is temporarily unavailable.",
    ),
    (
        ProviderRequestError,
        "PROVIDER_REQUEST_FAILED",
        "AI provider rejected the request.",
    ),
    (
        ProviderResponseError,
        "PROVIDER_INVALID_RESPONSE",
        "AI provider returned an invalid response.",
    ),
)


class Agent:
    """
    Agent 核心逻辑。

    比赛入口将 chat 交给统一 BusinessFlow 处理。
    原陪伴聊天路径仍可显式注入 Character 和 Composer 使用。
    """

    def __init__(
        self,
        provider: LLMProvider,
        name: str = "Desktop Companion",
        *,
        character: CharacterDefinition | None = None,
        composer: PromptContextComposer | None = None,
        clock: Clock,
        business_flow: "BusinessFlow | None" = None,
    ) -> None:
        if business_flow is None and (character is None or composer is None):
            raise ValueError("Legacy chat requires a character and composer")
        self.name = name
        self._provider = provider
        self._character = character
        self._composer = composer
        self._clock = clock
        self._business_flow = business_flow

    async def process_message(
        self,
        message: Message,
    ) -> Message:
        """
        异步处理输入消息并返回统一 Message。
        """

        if message.type == "chat":
            if self._business_flow is not None:
                return await self._business_flow.process_message(message)
            assert self._composer is not None and self._character is not None
            user_text = message.payload.get(
                "message",
                "",
            )

            current_datetime = self._clock.now()

            temporal_context = TemporalContext(
                current_datetime=current_datetime,
            )

            request = self._composer.compose(
                character=self._character,
                temporal_context=temporal_context,
                user_message=user_text,
            )

            try:
                provider_response = await self._provider.generate(
                    request,
                )

            except LLMProviderError as exc:
                logger.warning(
                    "LLM provider request failed: %s",
                    type(exc).__name__,
                )

                code, safe_message = self._map_provider_error(
                    exc,
                )

                return Message(
                    type="error",
                    source=self.name,
                    payload={
                        "code": code,
                        "message": safe_message,
                    },
                )

            return Message(
                type="response",
                source=self.name,
                payload={
                    "message": provider_response.content,
                },
            )

        return Message(
            type="error",
            source=self.name,
            payload={
                "message": "Unsupported message type",
            },
        )

    @staticmethod
    def _map_provider_error(
        error: LLMProviderError,
    ) -> tuple[str, str]:
        """
        将 Provider-neutral error
        转换为稳定的 Desktop protocol code/message。
        """

        for (
            error_type,
            code,
            safe_message,
        ) in _PROVIDER_ERROR_PROTOCOL_MAP:
            if isinstance(
                error,
                error_type,
            ):
                return (
                    code,
                    safe_message,
                )

        return (
            "PROVIDER_ERROR",
            "AI provider request failed.",
        )
