# Copyright (c) 2025-2026 Telomere LLC. All rights reserved.
# Proprietary and confidential. See LICENSE file in the project root.

from functools import lru_cache
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── LLM Provider ─────────────────────────────────────────────────────────
    # Options: "bedrock", "openai", "gemini"
    llm_provider: str = "openai"

    # ── AWS Bedrock ──────────────────────────────────────────────────────────
    aws_default_region: str = "us-east-1"
    aws_access_key_id: str = ""
    aws_secret_access_key: SecretStr = SecretStr("")
    aws_profile: str = ""
    # Cross-region inference profile prefix: us.anthropic.claude-3-7-sonnet-20250219-v1:0
    bedrock_model_id: str = "anthropic.claude-3-7-sonnet-20250219-v1:0"
    bedrock_max_tokens: int = 4096
    bedrock_temperature: float = 0.1

    # ── OpenAI ───────────────────────────────────────────────────────────────
    openai_api_key: SecretStr = SecretStr("")
    openai_model_id: str = "gpt-5.2"
    openai_max_tokens: int = 4096
    openai_temperature: float = 0.1

    # ── Google Gemini ────────────────────────────────────────────────────────
    gemini_api_key: SecretStr = SecretStr("")
    gemini_model_id: str = "gemini-2.5-pro"  # or "gemini-3.1-pro"
    gemini_max_tokens: int = 4096
    gemini_temperature: float = 0.1

    # ── Jira ─────────────────────────────────────────────────────────────────
    jira_url: str = ""
    jira_username: str = ""
    jira_api_token: SecretStr = SecretStr("")
    jira_projects_filter: str = ""
    jira_poll_jql: str = 'status = "Ready for Dev" ORDER BY created DESC'
    jira_poll_interval_seconds: int = 300

    # ── GitHub (read / scouting — company repo, read-only) ───────────────────
    github_personal_access_token: SecretStr = Field(default=SecretStr(""), alias="GITHUB_PERSONAL_ACCESS_TOKEN")
    github_repo_owner: str = ""
    github_repo_name: str = ""
    github_base_branch: str = "main"
    github_default_reviewers: str = ""

    # ── GitHub (write / PR creation — personal demo repo) ────────────────────
    github_pr_token: SecretStr = Field(default=SecretStr(""), alias="GITHUB_PR_TOKEN")
    github_pr_repo_owner: str = ""
    github_pr_repo_name: str = ""

    # ── Persistence ──────────────────────────────────────────────────────────
    sqlite_db_path: str = "data/artoo.db"
    db_echo: bool = False

    # ── Logging ──────────────────────────────────────────────────────────────
    log_level: str = "INFO"
    activity_log_path: str = "logs/activity.jsonl"
    llm_log_path: str = "logs/llm_calls.jsonl"
    log_max_bytes: int = 10_000_000   # 10 MB before rotation
    log_backup_count: int = 5         # keep 5 rotated files

    # ── Metrics ──────────────────────────────────────────────────────────────
    metrics_port: int = 8080
    metrics_api_key: str = ""
    pr_reconcile_interval_seconds: int = 3600

    # ── Confluence ───────────────────────────────────────────────────────────
    confluence_url: str = ""           # e.g. https://your-org.atlassian.net/wiki
    confluence_space_keys: str = ""    # comma-separated space keys, e.g. "ENG,ARCH"
    confluence_max_pages: int = 10

    # ── Agent behaviour ───────────────────────────────────────────────────────
    repo_scout_max_files: int = 20
    completeness_threshold: float = 0.25
    llm_parse_retry_count: int = 3

    # ── Development ──────────────────────────────────────────────────────────
    dry_run: bool = False
    # Suppress all Jira writes (comments, labels) independently of dry_run.
    # Use when Jira is production but GitHub PR creation should still be live.
    jira_read_only: bool = False

    # ── Derived helpers ──────────────────────────────────────────────────────
    @property
    def github_token(self) -> str:
        return self.github_personal_access_token.get_secret_value()

    @property
    def default_reviewers_list(self) -> list[str]:
        return [r.strip() for r in self.github_default_reviewers.split(",") if r.strip()]

    @property
    def jira_projects_list(self) -> list[str]:
        return [p.strip() for p in self.jira_projects_filter.split(",") if p.strip()]

    @property
    def confluence_space_keys_list(self) -> list[str]:
        return [s.strip() for s in self.confluence_space_keys.split(",") if s.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
