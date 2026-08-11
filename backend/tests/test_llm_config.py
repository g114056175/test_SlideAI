import asyncio
import threading
import time

from backend.app.services.utility import api as llm_api


def _clear_llm_env(monkeypatch):
    for name in (
        "api_key",
        "LLM_PROVIDER",
        "GOOGLE_API_KEY",
        "GEMINI_API_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "OPENROUTER_API_KEY",
        "XAI_API_KEY",
        "GROQ_API_KEY",
        "CUSTOM_LLM_API_KEY",
        "EXTERNAL_LLM_API_KEY",
        "CUSTOM_LLM_ENDPOINT",
        "CUSTOM_LLM_MODEL",
    ):
        monkeypatch.delenv(name, raising=False)


def test_custom_openai_compatible_provider_can_run_without_api_key(monkeypatch):
    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER", "custom")
    monkeypatch.setenv("CUSTOM_LLM_ENDPOINT", "http://127.0.0.1:8081/v1/chat/completions")
    monkeypatch.setenv("CUSTOM_LLM_MODEL", "local-alias")
    captured = {}

    class DummyResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": "本地模型產生的講稿。"}}]}

    def fake_post(url, *, headers, json, timeout):
        captured.update(url=url, headers=headers, payload=json, timeout=timeout)
        return DummyResponse()

    monkeypatch.setattr(llm_api.requests, "post", fake_post)

    assert llm_api.llm_is_configured() is True
    assert llm_api.get_configured_llm_provider() == "custom"
    result = asyncio.run(
        llm_api.generate_presentation_scripts(
            text_array=["測試頁面"],
            language="zh",
        )
    )

    assert result == ["本地模型產生的講稿。"]
    assert captured["url"].endswith("/v1/chat/completions")
    assert captured["payload"]["model"] == "local-alias"
    assert "Authorization" not in captured["headers"]


def test_explicit_provider_does_not_depend_on_key_prefix(monkeypatch):
    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "vendor-new-prefix-key")

    assert llm_api.get_configured_llm_provider() == "openai"
    assert llm_api.llm_is_configured() is True


def test_explicit_provider_prefers_its_own_key_over_legacy_key(monkeypatch):
    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("api_key", "legacy-google-key")
    monkeypatch.setenv("OPENAI_API_KEY", "selected-openai-key")

    assert llm_api.get_llm_api_key() == "selected-openai-key"


def test_google_provider_uses_new_genai_client(monkeypatch):
    _clear_llm_env(monkeypatch)
    captured = {"closed": False}

    class DummyModels:
        def generate_content(self, *, model, contents):
            captured.update(model=model, contents=contents)
            return type("Response", (), {"text": "這是一段足夠完整的測試講稿內容。"})()

    class DummyClient:
        models = DummyModels()

        def close(self):
            captured["closed"] = True

    def fake_client(*, api_key):
        captured["api_key"] = api_key
        return DummyClient()

    monkeypatch.setattr(llm_api.genai, "Client", fake_client)

    result = asyncio.run(llm_api.gemini_chat(
        text_array=["測試投影片"],
        script="unused",
        api_key="AQ.test-key",
        language="zh",
        model_name_override="gemini-test",
    ))

    assert result == ["這是一段足夠完整的測試講稿內容。"]
    assert captured["api_key"] == "AQ.test-key"
    assert captured["model"] == "gemini-test"
    assert "測試投影片" in captured["contents"]
    assert captured["closed"] is True


def test_global_llm_limit_is_shared_by_five_users(monkeypatch):
    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER", "custom")
    monkeypatch.setenv("CUSTOM_LLM_ENDPOINT", "http://127.0.0.1:8081/v1/chat/completions")
    monkeypatch.setenv("CUSTOM_LLM_MODEL", "local-alias")
    state = {"active": 0, "peak": 0}
    guard = threading.Lock()

    class DummyResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": "測試講稿內容足夠長。"}}]}

    def fake_post(*args, **kwargs):
        with guard:
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
        time.sleep(0.03)
        with guard:
            state["active"] -= 1
        return DummyResponse()

    monkeypatch.setattr(llm_api.requests, "post", fake_post)

    async def five_users():
        return await asyncio.gather(*(
            llm_api.generate_presentation_scripts(
                text_array=[f"使用者 {index} 第一頁", f"使用者 {index} 第二頁"],
                language="zh",
            )
            for index in range(5)
        ))

    results = asyncio.run(five_users())
    assert len(results) == 5
    assert state["peak"] <= 3
    assert state["peak"] >= 2
