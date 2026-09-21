from types import SimpleNamespace
import time

import pytest
from openpilot.bluepilot.selfdrive.car import bp_card_publisher as publisher


def test_controller_latched_settings_are_published(monkeypatch):
  monkeypatch.setattr(publisher, "_settings_cache", {"bmsPrimaryControlVariable": 1})
  monkeypatch.setattr(publisher, "_settings_last_read", time.monotonic())
  cc = SimpleNamespace(lateralUncertainty=0., disable_BP_lat_UI=False, primary_lateral_control=1,
                       smoothing_enabled=True, smoothing_strength=1.5, user_dampening_factor=1.05,
                       bp_pinion_curvature_enabled=True)
  ci = SimpleNamespace(CC=cc, CP=SimpleNamespace(carFingerprint="FORD_F_150_RAPTOR_MK3", fingerprintSource="fixed"))
  sent = []
  pm = SimpleNamespace(send=lambda topic, message: sent.append((topic, message)))
  publisher.publish_controller_state_bp(ci, pm)
  topic, message = sent[0]
  assert topic == "controllerStateBP"
  cs = message.controllerStateBP
  assert cs.angleSmoothingEnabled
  assert cs.angleSmoothingStrength == pytest.approx(1.5)
  assert cs.angleLowCurveFactor == pytest.approx(1.05)
  assert cs.pinionCurvatureEnabled
  assert cs.bmsFingerprint == "FORD_F_150_RAPTOR_MK3"
  assert str(cs.activeLateralMode) == "angle"


@pytest.mark.parametrize("mode", [0, 1])
def test_angle_snapshot_round_trips_and_is_hidden_in_other_modes(monkeypatch, mode):
  from dataclasses import fields
  from cereal import custom, log
  from opendbc.car import structs

  monkeypatch.setattr(publisher, "_settings_cache", {"bmsPrimaryControlVariable": mode})
  monkeypatch.setattr(publisher, "_settings_last_read", time.monotonic())
  snapshot = structs.FordAngleDiagnostics(valid=True, controlMonoTime=1234567890, controlFrame=125,
                                        modelMonoTime=1200000000, delay=.32, delaySource="fixed",
                                        desiredCurvature=.0002, rawPredictedCurvature=.0003,
                                        pathAngle=.004, predictionHorizon=.38)
  cc = SimpleNamespace(lateralUncertainty=0., disable_BP_lat_UI=False, primary_lateral_control=mode,
                       bp_angle_diagnostics=snapshot)
  sent = []
  publisher.publish_controller_state_bp(SimpleNamespace(CC=cc), SimpleNamespace(send=lambda _, msg: sent.append(msg)))
  # Serialize all the way through capnp to catch undeclared/dropped dataclass fields.
  with log.Event.from_bytes(sent[0].to_bytes()) as event:
    decoded = event.controllerStateBP.angleDiagnostics
    assert decoded.valid == (mode == 1)
    assert set(custom.FordAngleDiagnostics.schema.fields) == {f.name for f in fields(snapshot)}
    if mode == 1:
      assert decoded.controlMonoTime == snapshot.controlMonoTime
      assert decoded.delaySource == "fixed"
      assert decoded.delay == pytest.approx(.32)
      assert decoded.pathAngle == pytest.approx(.004)
    else:
      assert decoded.pathAngle == 0.0
