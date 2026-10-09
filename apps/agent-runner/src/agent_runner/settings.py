from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# Prefer repo-root .env regardless of cwd
_ROOT_ENV = Path(__file__).resolve().parents[4] / ".env"
_ENV_FILES = (str(_ROOT_ENV), ".env") if _ROOT_ENV.exists() else (".env",)


class RunnerSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=_ENV_FILES, env_file_encoding="utf-8", extra="ignore")

    api_base: str = "http://127.0.0.1:8000"
    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    openai_model: str = "gpt-4o-mini"
    stock_type: str = "etf"  # stock | etf | convertible_bond
    universe_index: str = "000300"  # only used when stock_type=stock
    screen_size: int = 30
    intraday_interval_seconds: int = 300  # 盘中轮间隔，默认 5 分钟
    intraday_max_tool_turns: int = 8  # 盘中 agent loop 最大工具轮次
    postclose_max_tool_turns: int = 8  # 盘后复盘 agent loop 最大工具轮次
    actor: str = "agent-runner"