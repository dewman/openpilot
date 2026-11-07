"""Exercise BP paths that previously failed only after transitioning onroad."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


def test_card_import_and_parameter_conversion():
  from openpilot.selfdrive.car.card import Car
  from openpilot.selfdrive.car.helpers import convert_to_capnp
  from opendbc.car import structs
  assert Car is not None
  assert convert_to_capnp(structs.CarParamsSP()).to_dict() is not None
  assert convert_to_capnp(structs.CarStateSP()).to_dict() is not None


def test_other_driving_process_entry_points_import():
  from openpilot.selfdrive.controls.controlsd import Controls
  from openpilot.selfdrive.selfdrived.selfdrived import SelfdriveD
  assert Controls is not None and SelfdriveD is not None


def test_entire_settings_snapshot_matches_cereal_schema(monkeypatch):
  from openpilot.bluepilot.selfdrive.car import bp_card_publisher as publisher
  from openpilot.cereal import custom
  monkeypatch.setattr(publisher, 'Params', lambda: SimpleNamespace(get_bool=lambda key: False, get=lambda *a, **kw: None))
  snapshot = publisher._refresh_settings_cache()
  state = custom.ControllerStateBP.new_message(**snapshot)
  assert set(snapshot).issubset(state.to_dict())


@pytest.mark.parametrize('disabled,primary,expected', [(True, 0, 'openpilot'), (False, 0, 'curvature'), (False, 1, 'angle')])
def test_controller_state_uses_real_cereal_schema(monkeypatch, disabled, primary, expected):
  from openpilot.bluepilot.selfdrive.car import bp_card_publisher as publisher
  monkeypatch.setattr(publisher, '_refresh_settings_cache', lambda: {'bmsCenteringPidGain': 3.0})
  monkeypatch.setattr(publisher, '_settings_cache', {})
  ci = SimpleNamespace(
    CC=SimpleNamespace(lateralUncertainty=0.25, disable_BP_lat_UI=disabled, primary_lateral_control=primary),
    CP=SimpleNamespace(carFingerprint='FORD_F_150_RAPTOR_MK3', fingerprintSource='fixed'),
  )
  pm = Mock()
  publisher.publish_controller_state_bp(ci, pm)
  topic, message = pm.send.call_args.args
  assert topic == 'controllerStateBP' and message.valid
  state = message.controllerStateBP
  assert state.lateralUncertainty == pytest.approx(0.25)
  assert str(state.activeLateralMode) == expected
  assert state.bmsCenteringPidGain == pytest.approx(3.0)
  assert state.bmsFingerprintForced


@pytest.mark.parametrize('mici', [False, True])
@pytest.mark.parametrize('onroad,hidden,racer', [(True, True, False), (True, False, True), (False, True, True), (True, False, False)])
def test_hidden_camera_preserves_cabin_view(monkeypatch, mici, onroad, hidden, racer):
  from openpilot.cereal.visionipc import VisionStreamType
  if mici:
    from openpilot.selfdrive.ui.bp.mici.onroad import cameraview_bp as module
    cls = module.MiciCameraViewBP
  else:
    from openpilot.selfdrive.ui.bp.onroad import cameraview_bp as module
    cls = module.CameraViewBP
  monkeypatch.setattr(module, 'ui_state', SimpleNamespace(is_onroad=lambda: onroad))
  view = SimpleNamespace(_bp_hide_camera_view=hidden, _bp_rad_racer_theme=racer,
                         _stream_type=VisionStreamType.VISION_STREAM_NARROW_ROAD)
  assert cls._should_hide_camera_view(view) == (onroad and (hidden or racer))
  view._stream_type = VisionStreamType.VISION_STREAM_CABIN
  assert not cls._should_hide_camera_view(view)
