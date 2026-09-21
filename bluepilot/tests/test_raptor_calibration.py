"""Changing model identity resets vehicle learners without touching camera calibration."""
import pytest
from cereal import car, messaging
from openpilot.common.params import Params
from openpilot.selfdrive.locationd.paramsd import retrieve_initial_vehicle_params
from openpilot.selfdrive.locationd.lagd import retrieve_initial_lag


@pytest.mark.parametrize("previous", ["FORD_F_150_MK14", "FORD_F_150_RAPTOR_MK3"])
def test_model_identity_migration(tmp_path, previous):
  params = Params(str(tmp_path))
  cp = car.CarParams.new_message(carFingerprint="FORD_F_150_RAPTOR_MK3", steerRatio=17.2)
  old = car.CarParams.new_message(carFingerprint=previous, steerRatio=17.)
  params.put("CarParamsPrevRoute", old.to_bytes(), block=True)
  lp = messaging.new_message("liveParameters")
  lp.liveParameters.steerRatio = 18.1
  lp.liveParameters.stiffnessFactor = 1.
  lp.liveParameters.angleOffsetAverageDeg = .3
  params.put("LiveParametersV2", lp.to_bytes(), block=True)
  ld = messaging.new_message("liveDelay")
  ld.liveDelay.lateralDelayEstimate = .375
  ld.liveDelay.validBlocks = 3
  ld.liveDelay.status = "estimated"
  params.put("LiveDelay", ld.to_bytes(), block=True)
  camera = messaging.new_message("liveCalibration")
  camera.liveCalibration.height = [1.62]
  camera.liveCalibration.rpyCalib = [0., .117, .004]
  params.put("CalibrationParams", camera.to_bytes(), block=True)
  calibration_before = params.get("CalibrationParams")

  ratio, _, offset, _ = retrieve_initial_vehicle_params(params, cp, replay=True, debug=False)
  lag = retrieve_initial_lag(params, cp)
  if previous == cp.carFingerprint:
    assert ratio == pytest.approx(18.1)
    assert offset == pytest.approx(.3)
    assert lag == pytest.approx((.375, 3))
  else:
    assert ratio == pytest.approx(17.2)
    assert offset == 0.
    assert lag is None
    assert params.get("LiveParametersV2") is None
    assert params.get("LiveDelay") is None
  assert params.get("CalibrationParams") == calibration_before
