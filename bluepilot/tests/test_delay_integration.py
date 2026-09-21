"""Check typed Params, cache compatibility, and the model diagnostic schema together."""
from types import SimpleNamespace
import time

import pytest
from cereal import log, custom
from openpilot.common.params import Params
from openpilot.sunnypilot.livedelay.helpers import select_lat_delay
from openpilot.sunnypilot.livedelay.lagd_toggle import LagdToggle


def test_native_params_and_cache_writer_agree(tmp_path, monkeypatch):
  from openpilot.sunnypilot.livedelay import lagd_toggle
  params = Params(str(tmp_path))
  monkeypatch.setattr(lagd_toggle, "Params", lambda: params)
  params.put_bool("LagdToggle", False, block=True)
  params.put("LagdToggleDelay", .1, block=True)
  params.put("LagdValueCache", .9, block=True)  # deliberately wrong previous-mode cache
  cp = SimpleNamespace(steerActuatorDelay=.22)
  message = log.Event.new_message()
  message.init('liveDelay').lateralDelay = .338
  writer = LagdToggle(cp)
  selected = select_lat_delay(params, message.liveDelay.lateralDelay, cp.steerActuatorDelay)
  assert selected.value == pytest.approx(.32)
  writer.update(message)
  assert writer.lag == pytest.approx(selected.value)
  wait_for_cache(params, .32)
  params.put_bool("LagdToggle", True, block=True)
  assert select_lat_delay(params, .338, cp.steerActuatorDelay).value == .338
  writer.update(message)
  wait_for_cache(params, .338)


def test_model_diagnostics_identify_selected_timing_and_matching_frame():
  msg = custom.ModelDataV2SP.new_message(lateralDelay=.32, lateralDelaySource="fixed",
                                       lateralActionTime=.37, modelMonoTime=1234567890)
  with custom.ModelDataV2SP.from_bytes(msg.to_bytes()) as decoded:
    assert decoded.lateralDelay == pytest.approx(.32)
    assert decoded.lateralActionTime == pytest.approx(.37)
    assert decoded.lateralDelaySource == "fixed"
    assert decoded.modelMonoTime == 1234567890


def wait_for_cache(params, expected):
  # The production cache writer is deliberately nonblocking.
  deadline = time.monotonic() + 2.0
  while params.get("LagdValueCache") != pytest.approx(expected) and time.monotonic() < deadline:
    time.sleep(.01)
  assert params.get("LagdValueCache") == pytest.approx(expected)


def test_controls_poll_uses_current_vehicle_and_selected_mode(tmp_path):
  from openpilot.sunnypilot.selfdrive.controls.controlsd_ext import ControlsExt

  params = Params(str(tmp_path))
  params.put_bool("LagdToggle", False, block=True)
  params.put("LagdToggleDelay", .1, block=True)
  params.put("LagdValueCache", .9, block=True)
  controls = ControlsExt.__new__(ControlsExt)
  controls.params = params
  controls.CP = SimpleNamespace(steerActuatorDelay=.22)
  controls.blinker_pause_lateral = SimpleNamespace(get_params=lambda: None)
  controls._param_update_time = 0.
  controls.get_params_sp({"liveDelay": SimpleNamespace(lateralDelay=.338)})
  assert controls.lat_delay == pytest.approx(.32)
  assert controls.delay_settings.select(.4, .22).value == pytest.approx(.32)

  params.put_bool("LagdToggle", True, block=True)
  controls._param_update_time = 0.
  controls.get_params_sp({"liveDelay": SimpleNamespace(lateralDelay=.338)})
  assert controls.lat_delay == pytest.approx(.338)
  # Live control-loop timing must not wait another settings-poll interval.
  assert controls.delay_settings.select(.35, .22).value == pytest.approx(.35)
