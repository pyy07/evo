from functools import lru_cache
from pathlib import Path

import yaml
from pydantic_settings import BaseSettings, SettingsConfigDict


def _discover_root() -> Path:
    """Find repo root (contains config/instruments.yaml) or fall back to CWD."""
    here = Path(__file__).resolve()
    for parent in [here.parent, *here.parents]:
        candidate = parent / "config" / "instruments.yaml"
        if candidate.exists():
            return parent
    cwd = Path.cwd()
    if (cwd / "config" / "instruments.yaml").exists():
        return cwd
    return cwd


ROOT = _discover_root()
_ENV_FILES = tuple(
    p for p in (str(ROOT / ".env"), ".env") if Path(p).exists()
) or (".env",)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_ENV_FILES,
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = f"sqlite:///{ROOT / 'evo.db'}"
    market_data_mode: str = "mock"
    admin_token: str = "dev-admin-token"
    initial_cash: float = 1_000_000.0
    cors_origins: str = "http://localhost:5173,http://localhost:3000"
    instruments_config: str = str(ROOT / "config" / "instruments.yaml")


@lru_cache
def get_settings() -> Settings:
    return Settings()


@lru_cache
def load_instruments() -> dict:
    path = Path(get_settings().instruments_config)
    if not path.exists():
        # Packaged fallback defaults
        return {
            "default_cash": get_settings().initial_cash,
            "currency": "CNY",
            "paper_trading_only": True,
            "kinds": {
                "etf": {
                    "allow_short": False,
                    "lot_size": 100,
                    "commission_rate": 0.0001,
                    "min_commission": 0.0,
                    "code_prefixes": [
                        "15",
                        "16",
                        "51",
                        "56",
                        "58",
                        "159",
                        "510",
                        "511",
                        "512",
                        "513",
                        "515",
                        "516",
                        "518",
                        "560",
                        "561",
                        "562",
                        "563",
                        "588",
                    ],
                }
            },
        }
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f)