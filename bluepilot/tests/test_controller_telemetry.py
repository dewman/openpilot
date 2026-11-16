"""Controller status reaches the real schema without touching control commands."""
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from bluepilot.selfdrive.car import bp_card_publisher as publisher
from openpilot.cereal import custom, log
from opendbc.bluepilot_lateral.host_types import LateralStatus
from opendbc.car import structs


class Publisher:
  def __init__(self):
    self.messages = []

  def send(self, name, message):
    self.messages.append((name, log.Event.from_bytes(message.to_bytes())))


@pytest.fixture
def settings(monkeypatch):
  snapshot = Mock(return_value={'bmsShowBlueCruiseUiOnCluster': True})
  monkeypatch.setattr(publisher, '_refresh_settings_cache', snapshot)
  monkeypatch.setattr(publisher, '_settings_cache', {})
  monkeypatch.setattr(publisher, '_settings_last_read', None)
  monkeypatch.setattr(publisher.time, 'monotonic', lambda: 100.)
  return snapshot


def car(status):
  cp = structs.CarParams(carFingerprint='FORD_F_150_MK14', fingerprintSource='fixed')
  return SimpleNamespace(CC=SimpleNamespace(bp=SimpleNamespace(status=lambda: status)), CP=cp)


def test_extracted_status_is_published_with_cached_settings(settings):
  status = LateralStatus(lateralUncertainty=0.25, angleRateLimited=True, curvatureRateLimited=True,
                         curvatureDeviationLimited=True, humanTurnLateralPaused=True, stallBlipActive=True,
                         primary_lateral_control=1)
  ci = car(status)
  pm = Publisher()
  publisher.refresh_controller_settings_bp()
  publisher.publish_controller_state_bp(ci, pm)
  publisher.publish_controller_state_bp(ci, pm)
  settings.assert_called_once()
  assert len(pm.messages) == 2
  name, event = pm.messages[0]
  with event as msg:
    assert name == 'controllerStateBP' and msg.valid
    state = msg.controllerStateBP
    assert state.lateralUncertainty == pytest.approx(0.25)
    assert state.angleRateLimited and state.curvatureRateLimited and state.curvatureDeviationLimited
    assert state.humanTurnLateralPaused and state.stallBlipActive
    assert state.activeLateralMode == custom.ControllerStateBP.LateralMode.angle
    assert state.bmsShowBlueCruiseUiOnCluster
    assert state.bmsFingerprintForced and state.bmsFingerprint == 'FORD_F_150_MK14'
  assert ci.CC.bp.status() == status
  assert set(vars(ci.CC)) == {'bp'}


@pytest.mark.parametrize('disabled,mode,expected', [(True, 1, 'openpilot'), (False, 0, 'curvature'), (False, 1, 'angle')])
def test_reports_actual_controller_mode(settings, disabled, mode, expected):
  pm = Publisher()
  publisher.publish_controller_state_bp(car(LateralStatus(disable_BP_lat_UI=disabled, primary_lateral_control=mode)), pm)
  with pm.messages[0][1] as msg:
    assert str(msg.controllerStateBP.activeLateralMode) == expected


def test_unsupported_controller_does_not_read_settings_or_publish(settings):
  pm = Publisher()
  publisher.publish_controller_state_bp(SimpleNamespace(CC=SimpleNamespace()), pm)
  assert not pm.messages
  settings.assert_not_called()


def test_preserves_legacy_flat_status(settings):
  pm = Publisher()
  publisher.publish_controller_state_bp(SimpleNamespace(CC=LateralStatus(lateralUncertainty=0.125)), pm)
  with pm.messages[0][1] as msg:
    assert msg.controllerStateBP.lateralUncertainty == pytest.approx(0.125)


def test_settings_refresh_stays_out_of_most_control_cycles(settings, monkeypatch):
  pm = Publisher()
  ci = car(LateralStatus())
  publisher.refresh_controller_settings_bp()
  publisher.publish_controller_state_bp(ci, pm)
  monkeypatch.setattr(publisher.time, 'monotonic', lambda: 104.99)
  publisher.refresh_controller_settings_bp()
  publisher.publish_controller_state_bp(ci, pm)
  settings.assert_called_once()
  monkeypatch.setattr(publisher.time, 'monotonic', lambda: 105.)
  publisher.refresh_controller_settings_bp()
  publisher.publish_controller_state_bp(ci, pm)
  assert settings.call_count == 2


def test_blocked_settings_read_cannot_block_telemetry(settings, monkeypatch):
  publisher.refresh_controller_settings_bp()
  entered, release, published = threading.Event(), threading.Event(), threading.Event()

  def read():
    entered.set()
    assert release.wait(2)
    return {'bmsShowBlueCruiseUiOnCluster': False}

  monkeypatch.setattr(publisher, '_refresh_settings_cache', read)
  monkeypatch.setattr(publisher.time, 'monotonic', lambda: 106.)
  worker = threading.Thread(target=publisher.refresh_controller_settings_bp)
  pm = Publisher()

  def publish():
    publisher.publish_controller_state_bp(car(LateralStatus()), pm)
    published.set()

  control = threading.Thread(target=publish)
  worker.start()
  try:
    assert entered.wait(1)
    control.start()
    assert published.wait(1), 'telemetry waited for background Params I/O'
    with pm.messages[0][1] as msg:
      assert msg.controllerStateBP.bmsShowBlueCruiseUiOnCluster
  finally:
    release.set()
    worker.join(1)
    if control.ident is not None:
      control.join(1)
  assert not worker.is_alive() and not control.is_alive()


def test_failed_settings_refresh_retains_snapshot_and_rate_limit(settings, monkeypatch):
  publisher.refresh_controller_settings_bp()
  settings.side_effect = OSError('parameter store temporarily unavailable')
  monkeypatch.setattr(publisher.time, 'monotonic', lambda: 106.)
  publisher.refresh_controller_settings_bp()
  publisher.refresh_controller_settings_bp()
  assert settings.call_count == 2
  pm = Publisher()
  publisher.publish_controller_state_bp(car(LateralStatus()), pm)
  with pm.messages[0][1] as msg:
    assert msg.controllerStateBP.bmsShowBlueCruiseUiOnCluster
