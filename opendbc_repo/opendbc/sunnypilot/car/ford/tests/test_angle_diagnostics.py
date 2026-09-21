"""Validate actual steering stages, selected timing, and clearing on inactive paths."""
import math

import numpy as np
import pytest

from opendbc.sunnypilot.car.ford.tests.test_lateral_angle_ext import (
  _Actuators, _CC, _CS, _ForcedDetector, _Harness, _Model, _explorer_cp,
)
from openpilot.sunnypilot.livedelay.helpers import DelaySettings
from selfdrive.modeld.constants import ModelConstants


def harness():
  cp = _explorer_cp()
  cp.steerActuatorDelay = .22
  ext = _Harness(cp)
  ext.human_turn_detector = _ForcedDetector(False)
  ext.frame = 100
  ext.sm.logMonoTime = {"modelV2": 980_000_000, "liveDelay": 900_000_000}
  ext.model = _Model()
  ext.model.orientationRate.z = [t * .02 for t in ModelConstants.T_IDXS]
  ext.smoothing_enabled = False
  return ext


def drive(ext, active=True, desired=.0002, now=1_000_000_000):
  return ext.update_angle_strategy(_CC(active), _CS(vEgoRaw=20., vEgo=20.),
                                   _Actuators(desired), ext.CP, now)


def test_snapshot_matches_actual_stages_and_output():
  ext = harness()
  result = drive(ext)
  d = ext.bp_angle_diagnostics
  assert d.valid and d.controlMonoTime == 1_000_000_000 and d.controlFrame == 100
  assert d.modelMonoTime == 980_000_000 and d.liveDelayMonoTime == 900_000_000
  assert d.modelAge == pytest.approx(.02)
  assert d.delaySource == "live" and d.delay == .2
  assert d.decisionHorizon == pytest.approx(.20)
  assert d.predictionHorizon == pytest.approx(.25 + .1 * np.interp(20., [11.176, 24.5872], [1., 0.]))
  assert d.rawPredictedCurvature == pytest.approx(d.predictionHorizon * .001)
  assert d.predictedCurvature == d.rawPredictedCurvature
  assert d.blendedCurvature == pytest.approx(.5 * (.0002 + d.predictedCurvature))
  assert d.curvatureBeforeClip == d.blendedCurvature
  assert d.commandedCurvature == ext.bp_kappa_cmd
  assert d.pathAngleBeforeLimits == pytest.approx(d.commandedCurvature * 20 * d.curvatureGain)
  assert d.pathAngle == result.path_angle == ext.path_angle_last
  assert d.measuredCurvature == 0.0
  assert not d.pinionFeedback


def test_fixed_delay_reaches_ford_prediction_without_removing_caps():
  ext = harness()
  ext.angle_delay_settings = DelaySettings(False, .05)  # .27 total versus .2 live
  drive(ext)
  d = ext.bp_angle_diagnostics
  assert d.delaySource == "fixed" and d.delay == pytest.approx(.27)
  assert d.decisionHorizon == pytest.approx(.20)
  assert d.predictionHorizon == pytest.approx(.32 + .1 * np.interp(20., [11.176, 24.5872], [1., 0.]))
  ext.angle_delay_settings = DelaySettings(False, .50)
  drive(ext)
  assert ext.bp_angle_diagnostics.delay == pytest.approx(.72)
  assert ext.bp_angle_diagnostics.predictionHorizon == pytest.approx(.35 + .1 * np.interp(20., [11.176, 24.5872], [1., 0.]))


@pytest.mark.parametrize("reason", ["inactive", "human_turn", "stall_blip"])
def test_inactive_paths_clear_prior_command_snapshot(reason):
  ext = harness()
  drive(ext)
  previous = ext.bp_angle_diagnostics
  assert previous.valid
  if reason == "human_turn":
    ext.human_turn_detector = _ForcedDetector(True)
  if reason == "stall_blip":
    ext.stall_blip_frames_left = 2
  result = drive(ext, active=reason != "inactive", now=1_050_000_000)
  assert not ext.bp_angle_diagnostics.valid
  assert ext.bp_angle_diagnostics.controlMonoTime == 1_050_000_000
  assert ext.bp_angle_diagnostics.pathAngle == 0.0 == result.path_angle
  assert ext.bp_angle_diagnostics.desiredCurvature == 0.0
  assert previous.valid  # the previous object was not mutated
  drive(ext)


def test_logging_does_not_feed_back_into_commands():
  clean, altered = harness(), harness()
  for i in range(200):
    desired = .001 * math.sin(i * .07)
    a = drive(clean, desired=desired)
    altered.bp_angle_diagnostics.delay = 999.
    altered.bp_angle_diagnostics.pathAngle = -999.
    b = drive(altered, desired=desired)
    assert a == b
