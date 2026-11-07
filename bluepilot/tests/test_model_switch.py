"""Real-schema handover tests with simulated clocks, messages and process failures."""
from copy import deepcopy
from types import SimpleNamespace
import signal

import pytest
from openpilot.cereal import log
from opendbc.car.structs import car
from openpilot.bluepilot.models.engagement import ModelSwitchEngagementBP
from openpilot.bluepilot.models.switch import (
  ACTIVE_KEYS, PENDING, TRANSACTION, STATUS, REENGAGE, ERROR, RETRY, PROCESSES, SERVICES,
  ModelSwitchCoordinatorBP, stop_for_switch,
)


class Params:
  def __init__(self):
    self.values = {}

  def get(self, key):
    return deepcopy(self.values.get(key))

  def get_bool(self, key):
    return bool(self.get(key))

  def put(self, key, value, block=False):
    self.values[key] = deepcopy(value)

  put_bool = put

  def remove(self, key):
    self.values.pop(key, None)


class Process:
  def __init__(self):
    self.proc = None
    self.stubborn = False
    self.uninterruptible = False
    self.stops = []
    self.signals = []
    self.shutting_down = False

  def start(self):
    assert not self.shutting_down
    self.proc = SimpleNamespace(is_alive=lambda: True)

  def stop(self, block=True):
    assert not block, 'Coordinator must never block manager on a process join'
    self.stops.append(block)
    if not self.stubborn:
      self.proc = None
      self.shutting_down = False
    elif self.proc is not None:
      self.shutting_down = True

  def signal(self, sig):
    self.signals.append(sig)
    if not self.uninterruptible:
      self.proc = None
      self.shutting_down = False


class Messages(dict):
  def __init__(self):
    super().__init__()
    self.bad = set()
    self.logMonoTime = {}
    for name in ('deviceState', *SERVICES):
      event = log.Event.new_message()
      self[name] = event.init(name)
      self.logMonoTime[name] = 0
    self['deviceState'].started = True
    self.cs = car.CarState(canValid=True)

  def all_checks(self, names):
    return not self.bad.intersection(names)

  def stamp(self, now):
    for name in self:
      self.logMonoTime[name] = int(now * 1e9)
    state = self['selfdriveStateSP']
    state.bpModelSwitchCruiseDisengaged = not self.cs.cruiseState.enabled
    state.bpModelSwitchCarStateMonoTime = int(now * 1e9)
    self['longitudinalPlan'].modelMonoTime = int(now * 1e9)
    self['controlsState'].lateralPlanMonoTime = int(now * 1e9)
    self['controlsState'].longitudinalPlanMonoTime = int(now * 1e9)


class Harness:
  def __init__(self, source='qcom'):
    self.now = 100.
    self.params = Params()
    self.source = source
    self.previous = {'ref': 'previous', 'runner': 'tinygrad'}
    self.candidate = {'ref': 'candidate', 'runner': 'tinygrad'}
    self.params.put(ACTIVE_KEYS[source], self.previous)
    self.params.put('ModelRunnerTypeCache', 1)
    self.processes = {name: Process() for name in (*PROCESSES, 'card', 'pandad', 'controlsd', 'selfdrived')}
    self.sm = Messages()
    self.sm['deviceState'].chestnutPresent = source == 'chestnut'
    self.sm['modelV2'].big = source == 'chestnut'
    self.cache_ready = True
    self.coordinator = self.make_coordinator()
    self.ensure_running()

  def make_coordinator(self):
    return ModelSwitchCoordinatorBP(self.params, self.processes, lambda: self.now, lambda *args: self.cache_ready)

  def ensure_running(self):
    if stop_for_switch(self.params):
      return
    runner = (self.params.get(ACTIVE_KEYS[self.source]) or {}).get('runner', 'stock')
    self.params.put('ModelRunnerTypeCache', 1 if runner == 'tinygrad' else 2)
    model = 'modeld_tinygrad' if runner == 'tinygrad' else 'modeld'
    for name in PROCESSES:
      if name in (model, 'plannerd'):
        if self.processes[name].proc is None:
          self.processes[name].start()
      else:
        self.processes[name].proc = None

  def request(self):
    self.params.put(PENDING, {'bundle': self.candidate, 'source': self.source, 'requested': self.now})

  def tick(self, dt=.5, ack=True, stamp=True):
    self.now += dt
    if stamp:
      self.sm.stamp(self.now)
    txn = self.params.get(TRANSACTION)
    if ack and txn:
      self.sm['selfdriveStateSP'].bpModelSwitchToken = txn['id']
    self.coordinator.update(self.sm)
    self.ensure_running()

  def loading(self):
    self.request()
    for _ in range(4):
      self.tick()
    assert self.params.get(TRANSACTION)['phase'] == 'loading'

  def finish(self):
    for _ in range(8):
      self.tick()


@pytest.mark.parametrize('source', ['qcom', 'chestnut'])
def test_switch_and_manual_reengagement(source):
  h = Harness(source)
  h.loading()
  h.finish()
  assert h.params.get(STATUS) == 'ready'
  assert h.params.get(TRANSACTION) is None
  assert h.params.get_bool(REENGAGE)
  assert h.params.get(ACTIVE_KEYS[source]) == h.candidate
  for name in ('card', 'pandad', 'controlsd', 'selfdrived'):
    assert not h.processes[name].stops


@pytest.mark.parametrize('kind', ['stock', 'mads', 'lat', 'long', 'cruise', 'stale', 'invalid'])
def test_request_cannot_stop_an_engaged_or_stale_graph(kind):
  h = Harness()
  if kind == 'stock':
    h.sm['selfdriveState'].enabled = True
  if kind == 'mads':
    h.sm['selfdriveStateSP'].mads.enabled = True
  if kind == 'lat':
    h.sm['carControl'].latActive = True
  if kind == 'long':
    h.sm['carControl'].longActive = True
  if kind == 'cruise':
    h.sm.cs.cruiseState.enabled = True
  if kind == 'invalid':
    h.sm.bad.add('carControl')
  h.request()
  h.tick(stamp=kind != 'stale')
  assert h.params.get(TRANSACTION) is None
  assert all(not p.stops for p in h.processes.values())
  h.tick(dt=6)
  assert h.params.get(PENDING) is None
  assert h.params.get(STATUS) == 'cancelled'


def test_lock_acknowledgement_requires_subsequent_inactive_control():
  h = Harness()
  h.request()
  h.tick()
  h.tick()
  assert h.params.get(TRANSACTION)['phase'] == 'locking'
  assert all(not p.stops for p in h.processes.values())
  h.tick()
  assert h.params.get(TRANSACTION)['phase'] == 'stopping'


def test_no_ack_expires_without_touching_current_model():
  h = Harness()
  h.request()
  h.tick()
  h.tick(dt=6, ack=False)
  assert h.params.get(STATUS) == 'cancelled'
  assert h.params.get(ACTIVE_KEYS['qcom']) == h.previous
  assert all(not p.stops for p in h.processes.values())


def test_stuck_process_escalates_without_blocking():
  h = Harness()
  proc = h.processes['modeld_tinygrad']
  proc.stubborn = True
  h.request()
  for _ in range(3):
    h.tick()
  h.tick(dt=3.1)
  assert proc.signals == [signal.SIGKILL]
  h.tick()
  h.finish()
  assert h.params.get(STATUS) == 'ready'


def test_unkillable_process_fails_closed_and_does_not_respawn():
  h = Harness()
  proc = h.processes['modeld_tinygrad']
  proc.stubborn = proc.uninterruptible = True
  h.request()
  for _ in range(3):
    h.tick()
  h.tick(dt=8)
  assert h.params.get(STATUS) == 'failed'
  assert stop_for_switch(h.params)
  assert h.params.get(ACTIVE_KEYS['qcom']) == h.previous


@pytest.mark.parametrize('failure', ['stale_device', 'invalid_model', 'stale_plan', 'nan', 'wrong_hardware'])
def test_output_failure_rolls_back_with_bounded_deadline(failure):
  h = Harness()
  h.loading()
  if failure == 'stale_device':
    h.sm.bad.add('deviceState')
  if failure == 'invalid_model':
    h.sm.bad.add('modelV2')
  if failure == 'stale_plan':
    h.sm['longitudinalPlan'].modelMonoTime = 0
  if failure == 'nan':
    h.sm['modelV2'].action.desiredCurvature = float('nan')
  if failure == 'wrong_hardware':
    h.sm['modelV2'].big = True
  h.tick(dt=31, stamp=failure != 'stale_plan')
  assert h.params.get(TRANSACTION)['restoring']
  h.sm.bad.clear()
  h.sm['modelV2'].big = False
  h.sm['modelV2'].action.desiredCurvature = 0
  h.tick()
  h.finish()
  assert h.params.get(STATUS) == 'restored'
  assert h.params.get(ACTIVE_KEYS['qcom']) == h.previous
  assert h.params.get(ERROR)


def test_failed_rollback_can_retry_without_reboot():
  h = Harness()
  h.loading()
  h.sm.bad.add('modelV2')
  h.tick(dt=31)
  h.tick()
  h.tick(dt=31)
  assert h.params.get(STATUS) == 'failed'
  h.sm.bad.clear()
  h.params.put_bool(RETRY, True)
  h.tick()
  h.finish()
  assert h.params.get(STATUS) == 'restored'
  assert h.params.get(TRANSACTION) is None


def test_manager_crash_restores_previous_bundle_under_persistent_lock():
  h = Harness()
  h.loading()
  h.coordinator = h.make_coordinator()
  assert h.params.get(TRANSACTION)['restoring']
  h.finish()
  assert h.params.get(STATUS) == 'restored'
  assert h.params.get(ACTIVE_KEYS['qcom']) == h.previous


def test_ignition_off_restores_previous_without_reboot():
  h = Harness()
  h.loading()
  h.sm['deviceState'].started = False
  h.tick()
  assert h.params.get(STATUS) == 'restored'
  assert h.params.get(ACTIVE_KEYS['qcom']) == h.previous


def test_removed_cache_before_activation_rolls_back():
  h = Harness()
  h.request()
  for _ in range(3):
    h.tick()
  h.cache_ready = False
  h.tick()
  h.tick()
  h.finish()
  assert h.params.get(STATUS) == 'restored'
  assert h.params.get(ACTIVE_KEYS['qcom']) == h.previous


def test_interlock_requires_fresh_press_after_unlock():
  params = Params()
  switch = ModelSwitchEngagementBP(params)
  cs = car.CarState()
  events = SimpleNamespace(add=lambda event: None)
  params.put(TRANSACTION, {'id': 'token'})
  switch.update(cs, events)
  assert switch.blocked
  cs.buttonEvents = [car.CarState.ButtonEvent(type='resumeCruise', pressed=True)]
  switch.update(cs, events)
  params.remove(TRANSACTION)
  switch.update(cs, events)
  assert switch.blocked
  cs.buttonEvents = [car.CarState.ButtonEvent(type='resumeCruise', pressed=False)]
  switch.update(cs, events)
  assert switch.blocked
  cs.buttonEvents = [car.CarState.ButtonEvent(type='resumeCruise', pressed=True)]
  switch.update(cs, events)
  assert not switch.blocked


def test_car_state_relay_rejects_invalid_and_missing_samples():
  params = Params()
  switch = ModelSwitchEngagementBP(params)
  sample = SimpleNamespace(valid=True, logMonoTime=123, carState=car.CarState(canValid=True))
  switch.observe_car_state(sample)
  assert switch.car_state_mono_time == 123 and switch.cruise_disengaged
  switch.observe_car_state(None)
  assert switch.car_state_mono_time == 0 and not switch.cruise_disengaged
  sample.carState.canTimeout = True
  switch.observe_car_state(sample)
  assert not switch.cruise_disengaged


def test_reengagement_latch_catches_transaction_missed_by_selfdrived():
  params = Params()
  switch = ModelSwitchEngagementBP(params)
  params.put_bool(REENGAGE, True)
  switch.update(car.CarState(), SimpleNamespace(add=lambda event: None))
  assert switch.blocked


def test_bp_exp_interrupted_transaction_uses_safe_default_for_obsolete_bundle():
  h = Harness()
  h.loading()
  txn = h.params.get(TRANSACTION)
  txn.pop('source')
  txn['previous']['minimumSelectorVersion'] = -1
  h.params.put(TRANSACTION, txn)
  h.coordinator = h.make_coordinator()
  h.finish()
  assert h.params.get(STATUS) == 'restored'
  assert h.params.get(ACTIVE_KEYS['qcom']) is None
