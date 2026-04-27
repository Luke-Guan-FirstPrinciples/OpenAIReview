"""Unit tests for provider routing."""

from reviewer import client


class _DummyOpenAI:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


def test_get_client_openai_provider_strips_openai_prefix(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.setattr(client, "OpenAI", _DummyOpenAI)
    client._provider_announced = False

    api_client, provider, prefix = client.get_client(
        provider="openai",
        model="openai/gpt-5.1",
    )

    assert provider == "openai"
    assert prefix == "openai/"
    assert api_client.kwargs == {"api_key": "test-key"}


def test_get_client_openai_provider_uses_openai_base_url(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://example.openai.test/v1")
    monkeypatch.setattr(client, "OpenAI", _DummyOpenAI)
    client._provider_announced = False

    api_client, provider, prefix = client.get_client(provider="openai")

    assert provider == "openai"
    assert prefix == "openai/"
    assert api_client.kwargs == {
        "api_key": "test-key",
        "base_url": "https://example.openai.test/v1",
    }
