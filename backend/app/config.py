from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # Application
    app_name: str = "TraceLab"
    version: str = "0.1.0"
    environment: str = "development"  # development | staging | production

    # Database — never log this value
    database_url: str = "sqlite+aiosqlite:///./tracelab.db"

    # LLM — secret, never appears in logs or prompts
    llm_api_key: str = ""
    llm_model: str = "gpt-4o"
    llm_base_url: str = "https://api.openai.com/v1"

    # External integrations — secrets, env-only
    jira_base_url: str = ""
    jira_api_token: str = ""
    github_token: str = ""

    # Resource limits (used by later CPs)
    max_hypotheses: int = 3
    max_patch_attempts: int = 2
    max_command_timeout_seconds: int = 120
    flaky_default_runs: int = 50
    flaky_min_runs: int = 20
    flaky_max_runs: int = 100


# Module-level singleton — import this everywhere
settings = Settings()
