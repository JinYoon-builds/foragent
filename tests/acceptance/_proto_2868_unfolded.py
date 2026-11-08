"""Break-it variant of _proto_2868 (mode "unfolded"); load with -p _proto_2868_unfolded."""
import pytest

from _proto_2868 import install


@pytest.fixture(autouse=True)
def _proto(monkeypatch):
    install(monkeypatch, "unfolded")
