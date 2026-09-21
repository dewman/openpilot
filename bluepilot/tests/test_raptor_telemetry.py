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
