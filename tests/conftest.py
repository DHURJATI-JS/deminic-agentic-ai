import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from helpers import BASE, Client  # noqa: E402


def pytest_configure(config):
    try:
        httpx.get(BASE + "/healthz", timeout=20)
    except Exception:  # noqa: BLE001
        pytest.exit(f"Deminic is not running at {BASE} - start it with run.bat / ./run.sh first", returncode=3)


@pytest.fixture
def alice() -> Client:
    return Client().register("alice")


@pytest.fixture
def bob() -> Client:
    return Client().register("bob")


@pytest.fixture
def anon() -> Client:
    return Client()
