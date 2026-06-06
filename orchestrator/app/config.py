from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=("../.env", ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Insforge ---
    insforge_project_url: str = ""
    insforge_anon_key: str = ""
    insforge_service_role_key: str = ""

    # --- Google AI Studio (Gemini) ---
    google_api_key: str = ""
    gemini_model_researcher: str = "gemini-3.5-flash"
    gemini_model_synthesizer: str = "gemini-2.5-pro"

    # --- Runtime ---
    fixture_mode: bool = False
    orchestrator_log_level: str = "info"
    poll_interval_seconds: float = 1.5


settings = Settings()
