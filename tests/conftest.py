"""Keep unit tests independent of the user's .env and shell model selection."""
import os

import pytest


@pytest.fixture(autouse=True)
def isolated_aivo_environment(monkeypatch):
    # Server imports load .env. Isolate the whole mapping so newly loaded keys
    # cannot leak from an API test into later fake-model protocol tests.
    environment = {key: value for key, value in os.environ.items() if not key.startswith("AIVO_")}
    monkeypatch.setattr(os, "environ", environment)
