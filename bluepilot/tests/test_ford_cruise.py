"""Stock ACC must accept SET after braking without cancelling its rising edge."""
from types import SimpleNamespace

import pytest

from openpilot.bluepilot.selfdrive.ford_cruise import FordCruiseCancelBP


def state(enabled=True, **kwargs):
  return SimpleNamespace(cruiseState=SimpleNamespace(enabled=enabled), brakePressed=False, regenBraking=False,
                         accFaulted=False, steerFaultTemporary=False, steerFaultPermanent=False, **kwargs)


def test_brake_then_set_waits_for_ack_then_remains_engaged():
  guard = FordCruiseCancelBP()
  cs = state(False)
  assert not guard.update(False, cs, 900_000_000, 890_000_000, 910_000_000, True)
  cs.cruiseState.enabled = True
  # New ACC state, previous selfdrived decision: original code sent CANCEL here.
  assert not guard.update(True, cs, 1_000_000_000, 990_000_000, 1_005_000_000, True)
  # Matching selfdrived state enables cruise on the next cycle.
  assert not guard.update(False, cs, 1_010_000_000, 1_008_000_000, 1_015_000_000, True)
  # Subsequent real disable must still cancel immediately.
  assert guard.update(True, cs, 1_020_000_000, 1_018_000_000, 1_025_000_000, True)


@pytest.mark.parametrize('fault', ['brakePressed', 'regenBraking', 'accFaulted', 'steerFaultTemporary', 'steerFaultPermanent'])
def test_pedals_and_faults_cancel_immediately(fault):
  cs = state()
  setattr(cs, fault, True)
  assert FordCruiseCancelBP().update(True, cs, 1_000_000_000, 990_000_000, 1_005_000_000, True)


@pytest.mark.parametrize('car_time,ss_time,now,valid', [
  (1_000_000_000, 1_001_000_000, 1_005_000_000, True),  # newer disable decision
  (1_000_000_000, 990_000_000, 1_005_000_000, False),   # invalid input
  (1_000_000_000, 900_000_000, 1_005_000_000, True),    # stale selfdrived
  (1_000_000_000, 990_000_000, 1_050_000_000, True),    # expired acknowledgement
  (1_010_000_000, 990_000_000, 1_005_000_000, True),    # future timestamp
  (1_000_000_000, 0, 1_005_000_000, True),             # missing selfdrived
])
def test_untrusted_or_current_disable_is_never_suppressed(car_time, ss_time, now, valid):
  assert FordCruiseCancelBP().update(True, state(), car_time, ss_time, now, valid)


def test_new_car_samples_cannot_extend_deadline():
  guard = FordCruiseCancelBP()
  cs = state()
  assert not guard.update(True, cs, 1_000_000_000, 990_000_000, 1_005_000_000, True)
  assert guard.update(True, cs, 1_060_000_000, 990_000_000, 1_065_000_000, True)


def test_second_set_after_braking_gets_its_own_ack_window():
  guard = FordCruiseCancelBP()
  cs = state()
  assert not guard.update(True, cs, 1_000_000_000, 990_000_000, 1_005_000_000, True)
  cs.cruiseState.enabled = False
  assert not guard.update(False, cs, 2_000_000_000, 1_990_000_000, 2_005_000_000, True)
  cs.cruiseState.enabled = True
  assert not guard.update(True, cs, 3_000_000_000, 2_990_000_000, 3_005_000_000, True)
