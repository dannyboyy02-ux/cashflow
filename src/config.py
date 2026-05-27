"""Configuration loader for the cash flow project.

Reads .env (secrets and environment-specific paths) and config.yaml
(non-secret app config) into typed module-level constants. All other modules
import their settings from here rather than reading env vars directly.
"""
from pathlib import Path
import os

import yaml
from dotenv import load_dotenv

PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent

load_dotenv(PROJECT_ROOT / ".env")

with open(PROJECT_ROOT / "config.yaml") as _f:
    _yaml_config = yaml.safe_load(_f)

ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")
ONEDRIVE_DATA_PATH: Path = Path(os.getenv("ONEDRIVE_DATA_PATH", ""))
SQLITE_PATH: Path = PROJECT_ROOT / os.getenv("SQLITE_PATH", "data/cashflow.db")
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")

FORECAST_HORIZON_WEEKS: int = _yaml_config["forecast"]["horizon_weeks"]
DATA_DIR: Path = PROJECT_ROOT / _yaml_config["paths"]["data_dir"]
WORKBOOK_TEMPLATE: Path = PROJECT_ROOT / _yaml_config["paths"]["workbook_template"]
WORKBOOK_OUTPUT: Path = PROJECT_ROOT / _yaml_config["paths"]["workbook_output"]
LOG_FORMAT: str = _yaml_config["logging"]["format"]