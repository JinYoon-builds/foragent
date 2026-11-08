"""Break-it variant of _proto_2868 (mode "r1291"); load with -p _proto_2868_r1291."""
import pytest

from _proto_2868 import install


@pytest.fixture(autouse=True)
def _proto(monkeypatch):
    install(monkeypatch, "r1291")
