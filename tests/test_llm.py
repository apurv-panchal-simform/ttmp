"""The live chat client against a fake transport: JSON-mode messages, deployment mapping, failures, model modes,
reasoning effort, keep-alive transport and routing to a second resource."""
import json

import pytest

from tmp_pipeline.llm import (
    AzureChat,
    ChatConfig,
    ModelRequest,
    ModelUnavailableError,
    messages,
    model_client,
)
from tmp_pipeline.retrieval import prompts
from tmp_pipeline.transport import ServiceError


class FakeOpenAI:
    def __init__(self, reply=None, status=200):
        self.calls, self.reply, self.status = [], reply, status

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, headers, json.loads(body)))
        content = json.dumps(self.reply) if not isinstance(self.reply, str) else self.reply
        return self.status, {}, json.dumps({"model": "gpt-6-sol-2026", "usage": {"prompt_tokens": 5},
                                            "choices": [{"message": {"content": content}}]}).encode()


def test_azure_chat_sends_json_mode_messages_to_the_mapped_deployment():
    t = FakeOpenAI({"answer": "ok"})
    chat = AzureChat(ChatConfig("https://x.openai.azure.com", "secret", {"gpt-6-luna": "luna-dz"}), t)
    resp = chat.complete(prompts.q3_answer("Q?", "CTX", "Short", "now", []))
    assert resp.data == {"answer": "ok"} and resp.model == "gpt-6-sol-2026"
    method, url, headers, body = t.calls[0]
    assert "/deployments/luna-dz/chat/completions" in url and headers["api-key"] == "secret"
    assert body["reasoning_effort"] == "none"                               # answers are written without thinking
    assert body["response_format"] == {"type": "json_object"} and body["temperature"] == 0
    assert [m["role"] for m in body["messages"]] == ["system", "user"]
    img = messages(ModelRequest("X7", "1", "m", "look", images=(b"png",)))
    assert img[0]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_a_model_that_rejects_temperature_0_is_asked_again_without_it():
    calls = []

    def transport(method, url, headers, body):
        body = json.loads(body)
        calls.append(body)
        if "temperature" in body:
            return 400, {}, (b'{"error": {"message": "Unsupported value: \'temperature\' does not support 0", '
                             b'"param": "temperature", "code": "unsupported_value"}}')
        return 200, {}, json.dumps({"choices": [{"message": {"content": '{"answer": "ok"}'}}]}).encode()
    chat = AzureChat(ChatConfig("https://x", "k"), transport)
    assert chat.complete(prompts.q3_answer("Q?", "CTX", "Short", "now", [])).data == {"answer": "ok"}
    assert chat.complete(prompts.q3_answer("Q2?", "CTX", "Short", "now", [])).data == {"answer": "ok"}
    assert ["temperature" in c for c in calls] == [True, False, False]     # remembered for the deployment


@pytest.mark.parametrize("reply,status", [("not json", 200), ({"answer": "x"}, 400), ("[1, 2]", 200)])
def test_azure_chat_failures_become_model_unavailable(reply, status):
    chat = AzureChat(ChatConfig("https://x", "k"), FakeOpenAI(reply, status))
    with pytest.raises(ModelUnavailableError):
        chat.complete(prompts.q3_answer("Q?", "CTX", "Short", "now", []))


def test_model_modes(tmp_path, monkeypatch):
    assert model_client("off") is None
    with pytest.raises(ValueError):
        model_client("replay")
    monkeypatch.delenv("AZURE_OPENAI_ENDPOINT", raising=False)
    with pytest.raises(ServiceError):
        model_client("live", tmp_path)
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://x")
    monkeypatch.setenv("AZURE_OPENAI_KEY", "k")
    monkeypatch.setenv("AZURE_OPENAI_CHAT_DEPLOYMENTS", "gpt-6-sol=a, gpt-6-luna=b")
    assert ChatConfig.from_env().deployment("gpt-6-luna") == "b"
    assert type(model_client("live", tmp_path / "fx", tmp_path / "cache")).__name__ == "CachedClient"


def test_reasoning_effort_is_sent_and_dropped_for_models_that_refuse_it():
    calls = []

    def t(method, url, headers, body):
        body = json.loads(body)
        calls.append(body)
        if "reasoning_effort" in body:
            return 400, {}, (b'{"error": {"message": "Unrecognized request argument supplied: reasoning_effort", '
                             b'"param": "reasoning_effort", "code": "unsupported_parameter"}}')
        return FakeOpenAI({"answer": "ok"})(method, url, headers, json.dumps(body).encode())

    chat = AzureChat(ChatConfig("https://x", "k", reasoning_effort="low"), t)
    chat.complete(prompts.q3_answer("Q?", "CTX", "Short", "now", []))
    chat.complete(prompts.q3_answer("Q2?", "CTX", "Short", "now", []))
    assert ["reasoning_effort" in c for c in calls] == [True, False, False]


def test_keepalive_transport_reuses_one_connection_across_threads(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    from tmp_pipeline import transport as tp
    opened = []

    class Conn:
        def __init__(self, host, timeout=None, context=None):
            opened.append(host)

        def request(self, method, path, body=None, headers=None):
            self.path = path

        def getresponse(self):
            r = type("R", (), {})()
            r.status, r.headers, r.will_close, r.read = 200, {}, False, lambda: self.path.encode()
            return r

        def close(self):
            pass

    monkeypatch.setattr(tp.http.client, "HTTPSConnection", Conn)
    t = tp.KeepAliveTransport()
    assert t("POST", "https://h.example/a?x=1", {}, b"")[2] == b"/a?x=1"
    for _ in range(3):
        with ThreadPoolExecutor(1) as pool:
            pool.submit(t, "POST", "https://h.example/b", {}, b"").result()
    assert opened == ["h.example"]


def test_a_routed_model_goes_to_its_own_resource(monkeypatch):
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://main.openai.azure.com/")
    monkeypatch.setenv("AZURE_OPENAI_KEY", "main-key")
    monkeypatch.setenv("AZURE_OPENAI_ROUTES", "gpt-6-luna=LUNA")
    monkeypatch.setenv("AZURE_OPENAI_LUNA_ENDPOINT", "https://other.openai.azure.com/")
    monkeypatch.setenv("AZURE_OPENAI_LUNA_KEY", "other-key")
    t = FakeOpenAI({"answer": "ok"})
    chat = AzureChat(ChatConfig.from_env(), t)
    chat.complete(prompts.q4_repair(["p"], "a", "Q?", "CTX", "now"))                  # gpt-6-sol: main resource
    chat.complete(ModelRequest("Q3", "1", "gpt-6-luna", "u", "s", (), {}))     # gpt-6-luna: routed
    (_, u1, h1, _), (_, u2, h2, _) = t.calls
    assert u1.startswith("https://main.openai.azure.com/") and h1["api-key"] == "main-key"
    assert u2.startswith("https://other.openai.azure.com/openai/deployments/gpt-6-luna/") and h2["api-key"] == "other-key"
    monkeypatch.delenv("AZURE_OPENAI_LUNA_KEY")
    with pytest.raises(ServiceError):
        ChatConfig.from_env()

