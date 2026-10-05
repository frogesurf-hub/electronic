import asyncio
from pathlib import Path

import pytest
from pydantic import SecretStr

from agent_core import main as main_module
from agent_core.characters import (
    CharacterNotFoundError,
)
from agent_core.config import Settings
from agent_core.core.business_flow import BusinessFlow
from agent_core.providers import ProviderConfigurationError
from agent_core.temporal import (
    SystemClock,
)


def test_default_active_character_is_builtin_aria() -> None:
    """
    验证历史角色加载助手仍可读取内置 Aria。
    """

    settings = _make_settings()

    character = main_module._load_active_character(
        settings,
    )

    assert character.character_id == "aria"
    assert character.display_name == "Aria"


def test_active_character_can_be_selected_from_configured_directory(
    tmp_path: Path,
) -> None:
    """
    验证外部 Character 目录和 active ID 可以由 Settings 指定。
    """

    character_path = tmp_path / "custom.toml"

    character_path.write_text(
        '''
character_id = "custom"
display_name = "Custom"

[identity]
description = """
A custom companion.
"""

[persona]
description = """
Calm.
"""

[speech_style]
description = """
Concise.
"""
'''.strip(),
        encoding="utf-8",
    )

    settings = _make_settings(
        character_definitions_dir=tmp_path,
        active_character_id="custom",
    )

    character = main_module._load_active_character(
        settings,
    )

    assert character.character_id == "custom"
    assert character.display_name == "Custom"


def test_unknown_active_character_id_is_rejected(
    tmp_path: Path,
) -> None:
    """
    验证历史角色加载助手拒绝不存在的 active Character。
    """

    character_path = tmp_path / "available.toml"

    character_path.write_text(
        '''
character_id = "available"
display_name = "Available"

[identity]
description = """
An available companion.
"""

[persona]
description = """
Calm.
"""

[speech_style]
description = """
Concise.
"""
'''.strip(),
        encoding="utf-8",
    )

    settings = _make_settings(
        character_definitions_dir=tmp_path,
        active_character_id="missing",
    )

    with pytest.raises(
        CharacterNotFoundError,
        match="missing",
    ):
        main_module._load_active_character(
            settings,
        )


def _make_settings(
    *,
    api_key: str | None = "test-secret-key",
    model_provider: str = "deepseek",
    character_definitions_dir: Path | None = None,
    active_character_id: str = "aria",
) -> Settings:
    """
    创建 Composition Root 测试使用的确定性 Settings。
    """

    secret = (
        SecretStr(
            api_key,
        )
        if api_key is not None
        else None
    )

    return Settings(
        app_name="Test Companion",
        environment="test",
        runtime_mode="cloud",
        websocket_host="127.0.0.1",
        websocket_port=9999,
        event_bus_queue_capacity=17,
        model_provider=model_provider,
        deepseek_api_key=secret,
        deepseek_model="deepseek-flash",
        deepseek_timeout_seconds=45.0,
        deepseek_thinking_enabled=False,
        log_level="DEBUG",
        character_definitions_dir=character_definitions_dir,
        active_character_id=active_character_id,
    )


def _install_runtime_fakes(
    monkeypatch: pytest.MonkeyPatch,
    calls: list[tuple[str, object]],
    settings: Settings,
    *,
    failure_at: str | None = None,
    failure: BaseException | None = None,
) -> None:
    # Real BusinessFlow wiring is exercised without network or database access.
    def record(name: str, value: object = None) -> None:
        calls.append((name, value))
        if name == failure_at:
            assert failure is not None
            raise failure

    class FakeProvider:
        def __init__(self, **kwargs):
            record("provider_init", kwargs)

        async def generate(self, request):
            raise AssertionError("Composition tests must not call the model")

        async def aclose(self):
            record("provider_close")

    class FakeAgent:
        def __init__(self, **kwargs):
            record("agent_init", kwargs)

    class FakeServer:
        def __init__(self, **kwargs):
            record("server_init", kwargs)

        async def run(self):
            record("server_run")

    def obsolete_character_loader(_settings):
        raise AssertionError("Query startup must not load companion characters")

    monkeypatch.setattr(main_module, "get_settings", lambda: settings)
    monkeypatch.setattr(main_module, "setup_logging", lambda **kw: None)
    monkeypatch.setattr(
        main_module, "_load_active_character", obsolete_character_loader
    )
    monkeypatch.setattr(main_module, "DeepSeekProvider", FakeProvider)
    monkeypatch.setattr(main_module, "Agent", FakeAgent)
    monkeypatch.setattr(main_module, "WebSocketServer", FakeServer)


def test_query_runtime_uses_shared_provider_and_business_flow(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, object]] = []
    # Even missing obsolete role files do not stop the query runtime.
    settings = _make_settings(
        character_definitions_dir=tmp_path / "missing",
        active_character_id="missing",
    )
    _install_runtime_fakes(monkeypatch, calls, settings)
    asyncio.run(main_module.run())
    assert [name for name, _ in calls] == [
        "provider_init",
        "agent_init",
        "server_init",
        "server_run",
        "provider_close",
    ]
    assert calls[0][1] == {
        "api_key": "test-secret-key",
        "model": "deepseek-flash",
        "timeout_seconds": 45.0,
        "thinking_enabled": False,
    }
    agent_args = calls[1][1]
    assert isinstance(agent_args, dict)
    assert agent_args["name"] == "Query Agent"
    assert isinstance(agent_args["clock"], SystemClock)
    assert isinstance(agent_args["business_flow"], BusinessFlow)
    assert "character" not in agent_args and "composer" not in agent_args
    server_args = calls[2][1]
    assert isinstance(server_args, dict)
    assert server_args["host"] == "127.0.0.1" and server_args["port"] == 9999


@pytest.mark.parametrize("failure_at", ["agent_init", "server_init", "server_run"])
def test_provider_is_closed_when_query_runtime_fails(
    monkeypatch: pytest.MonkeyPatch,
    failure_at: str,
) -> None:
    calls: list[tuple[str, object]] = []
    _install_runtime_fakes(
        monkeypatch,
        calls,
        _make_settings(),
        failure_at=failure_at,
        failure=RuntimeError("runtime failed"),
    )
    with pytest.raises(RuntimeError, match="runtime failed"):
        asyncio.run(main_module.run())
    assert calls[-1] == ("provider_close", None)
    assert sum(name == "provider_close" for name, _ in calls) == 1
    if failure_at != "server_run":
        assert not any(name == "server_run" for name, _ in calls)


def test_provider_is_closed_on_runtime_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, object]] = []
    _install_runtime_fakes(
        monkeypatch,
        calls,
        _make_settings(),
        failure_at="server_run",
        failure=asyncio.CancelledError(),
    )
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(main_module.run())
    assert calls[-1] == ("provider_close", None)


@pytest.mark.parametrize(
    "settings",
    [
        _make_settings(api_key=None),
        _make_settings(model_provider="unsupported"),
    ],
)
def test_invalid_provider_configuration_fails_before_server_start(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
) -> None:
    calls: list[tuple[str, object]] = []
    _install_runtime_fakes(monkeypatch, calls, settings)
    with pytest.raises(ProviderConfigurationError):
        asyncio.run(main_module.run())
    assert calls == []
