from bluepilot.backend.network import utils


class UnknownKeyName(Exception):
    pass


class StrictParams:
    """Like the real Params: reading a key that isn't declared raises."""

    def __init__(self, values):
        self._values = values

    def get_bool(self, key):
        if key not in self._values:
            raise UnknownKeyName(key)
        return self._values[key]


def test_onroad_when_device_is_onroad(monkeypatch):
    monkeypatch.setattr(utils, "params", StrictParams({"IsOffroad": False}))
    assert utils.is_onroad() is True


def test_offroad_when_device_is_offroad(monkeypatch):
    monkeypatch.setattr(utils, "params", StrictParams({"IsOffroad": True}))
    assert utils.is_onroad() is False


def test_assumes_onroad_when_state_cannot_be_read(monkeypatch, caplog):
    # Fail closed: a broken read must lock the portal, not unlock it.
    monkeypatch.setattr(utils, "params", StrictParams({}))
    assert utils.is_onroad() is True
    assert any(r.levelname == "WARNING" for r in caplog.records)


def test_preprocessor_only_processes_when_offroad_and_not_engaged(monkeypatch):
    import importlib
    from unittest.mock import patch
    # Import the background worker without reconfiguring the caller's logging.
    with patch('logging.basicConfig'):
        preprocessor = importlib.import_module('bluepilot.backend.routes.preprocessor')
    for offroad, driver_view, engaged, expected in [
        (False, False, False, False),
        (True, False, False, True),
        (True, True, False, False),
        (True, False, True, False),
    ]:
        monkeypatch.setattr(preprocessor, 'params', StrictParams({
            'IsOffroad': offroad, 'IsDriverViewEnabled': driver_view, 'IsEngaged': engaged,
        }))
        assert preprocessor.is_device_idle() is expected
    monkeypatch.setattr(preprocessor, 'params', StrictParams({}))
    assert preprocessor.is_device_idle() is False
