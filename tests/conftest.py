import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"

# Make `src` importable as a package when running `pytest` from the repo root.
sys.path.insert(0, str(ROOT))


@pytest.fixture(scope="session")
def config_dir() -> Path:
    return CONFIG_DIR


@pytest.fixture(scope="session")
def all_configs(config_dir: Path) -> dict:
    configs = {}
    for path in config_dir.glob("*.yaml"):
        with open(path, "r", encoding="utf-8") as f:
            configs[path.stem] = yaml.safe_load(f)
    return configs
