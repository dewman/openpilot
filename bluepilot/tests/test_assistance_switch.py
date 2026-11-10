"""Assistance disable requests must precede the configuration reload."""
from types import SimpleNamespace

import pytest

from openpilot.bluepilot.selfdrive.assistance_switch import AssistanceSwitchBP, toggle_assistance
from openpilot.bluepilot.models.engagement import ModelSwitchEngagementBP
from openpilot.bluepilot.models.switch import REENGAGE
from openpilot.bluepilot.tests.test_model_switch import Params, Messages
from openpilot.cereal import log
from opendbc.car.structs import car
from openpilot.selfdrive.selfdrived.events import Events
from openpilot.selfdrive.selfdrived.state import StateMachine


@pytest.mark.parametrize('enabled,offroad,passive,cycle', [
  (False, False, False, False), (True, False, True, True),
  (False, True, False, True), (False, False, True, True),
])
def test_toggle_callback_defers_active_disable(enabled, offroad, passive, cycle):
  params = Params()
  state = SimpleNamespace(params=params, CP=SimpleNamespace(passive=passive), is_offroad=lambda: offroad)
  toggle_assistance(enabled, state)
  assert params.get_bool('OnroadCycleRequested') == cycle
  assert params.get_bool(REENGAGE)


def test_engaged_disable_cancels_before_reload_and_requires_manual_reengagement():
  params = Params()
  params.put_bool('OpenpilotEnabledToggle', False)
  now = 100.
  switch = AssistanceSwitchBP(params, passive=False, clock=lambda: now)
  guard = ModelSwitchEngagementBP(params)
  sm = Messages()
  sm['carControl'].enabled = sm['carControl'].latActive = sm['carControl'].longActive = True
  machine = StateMachine()
  machine.state = log.SelfdriveState.OpenpilotState.enabled
  events = Events()
  car_state = SimpleNamespace(car_state_mono_time=int(now * 1e9), cruise_disengaged=False)
  sm.stamp(now)
  switch.update(events, sm, car_state)
  guard.update(car.CarState(), events)
  assert machine.update(events) == (False, False)
  assert switch.blocked and guard.blocked
  assert not params.get_bool('OnroadCycleRequested')
  now += .1
  sm.stamp(now)
  car_state.car_state_mono_time = int(now * 1e9)
  # controlsd is inactive, but factory cruise has not acknowledged cancellation.
  sm['carControl'].enabled = sm['carControl'].latActive = sm['carControl'].longActive = False
  switch.update(events, sm, car_state)
  assert not params.get_bool('OnroadCycleRequested')
  car_state.cruise_disengaged = True
  switch.update(events, sm, car_state)
  assert params.get_bool('OnroadCycleRequested') and params.get_bool(REENGAGE)
  params.put_bool('OnroadCycleRequested', False)
  switch.update(events, sm, car_state)
  assert not params.get_bool('OnroadCycleRequested')  # do not keep extending the cycle
  restarted = ModelSwitchEngagementBP(params)
  assert restarted.blocked


@pytest.mark.parametrize('invalid', ['control', 'stale_control', 'stale_car', 'lat', 'long', 'enabled'])
def test_reload_requires_fresh_inactive_feedback(invalid):
  params = Params()
  switch = AssistanceSwitchBP(params, passive=False, clock=lambda: 100.)
  sm = Messages()
  sm.stamp(100.1)
  car_state = SimpleNamespace(car_state_mono_time=int(100.1e9), cruise_disengaged=True)
  switch.update(Events(), sm, car_state)
  switch.clock = lambda: 100.2
  if invalid == 'control':
    sm.bad.add('carControl')
  elif invalid == 'stale_control':
    sm.logMonoTime['carControl'] = int(99e9)
  elif invalid == 'stale_car':
    car_state.car_state_mono_time = 0
  else:
    setattr(sm['carControl'], {'lat': 'latActive', 'long': 'longActive', 'enabled': 'enabled'}[invalid], True)
  switch.update(Events(), sm, car_state)
  assert switch.blocked
  assert not params.get_bool('OnroadCycleRequested')


def test_passive_startup_does_not_loop_reload():
  params = Params()
  switch = AssistanceSwitchBP(params, passive=True)
  switch.update(Events(), None, None)
  assert not switch.blocked
  assert not params.get_bool('OnroadCycleRequested')
