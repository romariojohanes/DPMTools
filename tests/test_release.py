import importlib.metadata

from tools.release_check import ROOT, pinned, verify


def test_release_dependencies_are_locked_and_installed():
    direct = pinned(ROOT / 'requirements.txt')
    locked = pinned(ROOT / 'requirements-lock.txt')
    assert direct
    assert {name: locked.get(name) for name in direct} == direct
    assert {name: importlib.metadata.version(name) for name in direct} == direct
    assert verify() == direct
