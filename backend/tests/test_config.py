"""Configuration picks up the Bedrock key without storing it in a prompt or file."""

from app.config import Settings


def test_bedrock_key_environment_alias(monkeypatch):
    monkeypatch.setenv("AWS_BEDROCK_API_TEST", "fake-bedrock-key")
    monkeypatch.setenv("LLM_API_KEY", "other-key")
    settings = Settings(_env_file=None)
    assert settings.llm_api_key == "fake-bedrock-key"
