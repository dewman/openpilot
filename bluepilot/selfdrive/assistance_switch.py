"""Disengage assistance before cycling into passive mode."""
import time

from openpilot.cereal import log
from openpilot.bluepilot.models.switch import REENGAGE


def toggle_assistance(enabled, state):
  # The UI writes OpenpilotEnabledToggle after this callback returns.
  # An active onroad selfdrived owns the disable/reload handshake.
  state.params.put_bool(REENGAGE, True, block=True)
  if enabled or state.is_offroad() or (state.CP is not None and state.CP.passive):
    state.params.put_bool('OnroadCycleRequested', True, block=True)


class AssistanceSwitchBP:
  def __init__(self, params, passive, clock=time.monotonic):
    self.params, self.passive, self.clock = params, passive, clock
    self.blocked = False
    self.requested_at = None
    self.cycle_requested = False

  def update(self, events, sm, car_state):
    self.blocked = not self.passive and not self.params.get_bool('OpenpilotEnabledToggle')
    if not self.blocked:
      self.requested_at = None
      self.cycle_requested = False
      return
    if self.requested_at is None:
      self.requested_at = self.clock()
      self.params.put_bool(REENGAGE, True, block=True)
    # Standard cancel alert and stock state-machine disable. MADS also checks
    # blocked explicitly, since its normal event processing removes buttonCancel.
    events.add(log.OnroadEvent.EventName.buttonCancel)
    now = self.clock()
    cc = sm['carControl']
    control_time = sm.logMonoTime['carControl'] / 1e9
    car_time = car_state.car_state_mono_time / 1e9
    inactive = (sm.all_checks(['carControl']) and
                self.requested_at < control_time <= now and now - control_time < 1 and
                self.requested_at < car_time <= now and now - car_time < 1 and
                car_state.cruise_disengaged and not (cc.enabled or cc.latActive or cc.longActive))
    if inactive and not self.cycle_requested:
      self.params.put_bool(REENGAGE, True, block=True)
      self.params.put_bool('OnroadCycleRequested', True, block=True)
      self.cycle_requested = True
