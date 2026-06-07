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

    # --- Extractor pool / Replicas integration ---
    # Off by default — the user-facing pipeline keeps working without it.
    # Flip to true once the Replicas GitHub app is installed on the target
    # repo so subprocess `replicas create` won't dead-end.
    replicas_enabled: bool = False
    replicas_cli_path: str = "/Users/andrewwang/.bun/bin/replicas"
    replicas_min_runs: int = 3
    replicas_target_repo: str = "adoozy2026/insforge-hack"
    # ID from `replicas env list`. Without it the CLI shows an interactive
    # "Select environment" picker and our subprocess hangs / no-ops because
    # we don't have a TTY attached. Find the right one by running:
    #   replicas env list
    # and copying the "Default <org>/<repo>" ID.
    replicas_environment_id: str = ""


settings = Settings()
