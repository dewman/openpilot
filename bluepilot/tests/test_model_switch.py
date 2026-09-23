"""Isolated handover tests: real schemas, simulated process and message lifecycles."""
from copy import deepcopy
from types import SimpleNamespace

import pytest
from cereal import car, custom, log
from openpilot.bluepilot.models.engagement import ModelSwitchEngagementBP
from openpilot.bluepilot.models.switch import (
  ACTIVE, CONTROL_SERVICES, OUTPUT_SERVICES, PENDING, PROCESSES, REENGAGE, SERVICES, STATUS, TRANSACTION,
  ModelSwitchCoordinatorBP, displayed_bundle, stage_bundle,
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

  def put_bool(self, key, value, block=False):
    self.put(key, value, block)

  def remove(self, key):
    self.values.pop(key, None)


class Process:
  def __init__(self):
    self.proc = SimpleNamespace(is_alive=lambda: True)
    self.stops = []

  def stop(self, block=True):
    self.stops.append(block)
    self.proc = None


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
    self.cs = car.CarState.new_message(vEgo=30., gearShifter='drive', canValid=True)

  def all_checks(self, names):
    return not self.bad.intersection(names)

  def stamp(self, now):
    for name in self:
      self.logMonoTime[name] = int(now * 1e9)
    self['selfdriveStateSP'].bpModelSwitchCruiseDisengaged = not self.cs.cruiseState.enabled
    self['selfdriveStateSP'].bpModelSwitchCarStateMonoTime = int(now * 1e9)
    self['longitudinalPlan'].modelMonoTime = int(now * 1e9)
    self['controlsState'].lateralPlanMonoTime = int(now * 1e9)
    self['controlsState'].longitudinalPlanMonoTime = int(now * 1e9)


class Harness:
  def __init__(self):
    self.now = 100.
    self.params = Params()
    self.previous = {'ref': 'previous', 'runner': 'tinygrad'}
    self.candidate = {'ref': 'candidate', 'runner': 'tinygrad'}
    self.params.put(ACTIVE, self.previous)
    self.params.put('ModelRunnerTypeCache', 1)
    self.processes = {name: Process() for name in (*PROCESSES, 'card', 'pandad', 'controlsd', 'selfdrived')}
    self.sm = Messages()
    self.coordinator = ModelSwitchCoordinatorBP(self.params, self.processes, lambda: self.now)
    self.ensure_running()

  def ensure_running(self):
    runner = (self.params.get(ACTIVE) or {}).get('runner', 'stock')
    model = 'modeld_tinygrad' if runner == 'tinygrad' else 'modeld'
    for name in PROCESSES:
      if name in (model, 'plannerd'):
        self.processes[name].proc = SimpleNamespace(is_alive=lambda: True)
      else:
        self.processes[name].proc = None

  def tick(self, dt=.5, ack=True, before=None):
    self.now += dt
    self.sm.stamp(self.now)
    txn = self.params.get(TRANSACTION)
    if ack and txn:
      self.sm['selfdriveStateSP'].bpModelSwitchToken = txn['id']
    if before:
      before()
    self.coordinator.update(self.sm)
    self.ensure_running()

  def begin(self, candidate=None):
    stage_bundle(self.params, self.candidate if candidate is None else candidate)
    self.tick()
    assert self.params.get(TRANSACTION)['phase'] == 'locking'

  def load(self):
    for _ in range(2):
      self.tick()
      if self.params.get(TRANSACTION)['phase'] == 'loading':
        break
    assert self.params.get(TRANSACTION)['phase'] == 'loading'

  def finish(self):
    for _ in range(5):
      self.tick()


def test_switch_while_moving_keeps_can_and_controls_running():
  h = Harness()
  h.begin()
  assert h.params.get(ACTIVE) == h.previous
  assert all(not p.stops for p in h.processes.values())
  h.load()
  assert h.params.get(ACTIVE) == h.candidate
  assert h.params.get('ModelRunnerTypeCache') is None
  assert displayed_bundle(h.params) == h.previous
  assert all(h.processes[n].stops == [False, True] for n in PROCESSES)
  h.finish()
  assert h.params.get(TRANSACTION) is None
  assert h.params.get(STATUS) == 'ready'
  assert h.params.get_bool(REENGAGE)
  assert displayed_bundle(h.params) == h.candidate
  assert all(not h.processes[n].stops for n in ('card', 'pandad', 'controlsd', 'selfdrived'))


@pytest.mark.parametrize('engagement', ['stock', 'mads', 'enabled', 'lateral', 'longitudinal', 'factory_cruise'])
def test_ready_download_waits_for_every_control_channel(engagement):
  h = Harness()
  if engagement == 'stock':
    h.sm['selfdriveState'].enabled = True
  elif engagement == 'mads':
    h.sm['selfdriveStateSP'].mads.enabled = True
  elif engagement == 'factory_cruise':
    h.sm.cs.cruiseState.enabled = True
  else:
    setattr(h.sm['carControl'], {'enabled': 'enabled', 'lateral': 'latActive', 'longitudinal': 'longActive'}[engagement], True)
  stage_bundle(h.params, h.candidate)
  h.tick()
  assert h.params.get(TRANSACTION) is None
  assert h.params.get(PENDING) is not None
  assert h.params.get(ACTIVE) == h.previous
  assert all(not p.stops for p in h.processes.values())


@pytest.mark.parametrize('service', CONTROL_SERVICES)
@pytest.mark.parametrize('fault', ['invalid', 'stale'])
def test_unknown_control_state_does_not_start_handover(service, fault):
  h = Harness()
  stage_bundle(h.params, h.candidate)
  if fault == 'invalid':
    h.sm.bad.add(service)
  h.tick(before=lambda: h.sm.logMonoTime.__setitem__(service, 0) if fault == 'stale' else None)
  assert h.params.get(TRANSACTION) is None
  assert all(not p.stops for p in h.processes.values())


def test_acknowledgement_required_even_if_telemetry_is_inactive():
  h = Harness()
  h.begin()
  h.sm['selfdriveStateSP'].bpModelSwitchToken = 'old-transaction'
  h.tick(ack=False)
  assert all(not p.stops for p in h.processes.values())
  h.tick(dt=11, ack=False)
  assert h.params.get(TRANSACTION)['phase'] == 'failed'
  assert h.params.get(ACTIVE) == h.previous


def test_engagement_race_after_request_does_not_restart_model():
  h = Harness()
  h.begin()
  h.sm['carControl'].latActive = True
  h.tick()
  assert h.params.get(TRANSACTION)['phase'] == 'locking'
  assert all(not p.stops for p in h.processes.values())


def test_inactive_commands_must_postdate_lock_acknowledgement():
  h = Harness()
  h.begin()
  h.tick()
  ack_time = h.sm.logMonoTime['selfdriveStateSP']
  h.tick(before=lambda: h.sm.logMonoTime.__setitem__('carControl', ack_time))
  assert h.params.get(TRANSACTION)['phase'] == 'locking'
  assert all(not p.stops for p in h.processes.values())
  h.tick()
  assert h.params.get(TRANSACTION)['phase'] == 'loading'


@pytest.mark.parametrize('service', OUTPUT_SERVICES)
def test_stale_pre_restart_outputs_cannot_unlock(service):
  h = Harness()
  h.begin()
  h.load()
  old = int(h.coordinator.started_at * 1e9)
  for _ in range(6):
    h.tick(before=lambda: h.sm.logMonoTime.__setitem__(service, old))
  assert h.params.get(TRANSACTION) is not None


@pytest.mark.parametrize('field', ['modelMonoTime', 'lateralPlanMonoTime', 'longitudinalPlanMonoTime'])
def test_planner_and_controls_must_consume_new_outputs(field):
  h = Harness()
  h.begin()
  h.load()
  obj = h.sm['longitudinalPlan' if field == 'modelMonoTime' else 'controlsState']
  for _ in range(6):
    h.tick(before=lambda: setattr(obj, field, 0))
  assert h.params.get(TRANSACTION) is not None


def test_health_must_be_continuous_and_finite():
  h = Harness()
  h.begin()
  h.load()
  h.tick()
  h.tick(1.)
  h.sm['modelV2'].action.desiredCurvature = float('nan')
  h.tick()
  h.sm['modelV2'].action.desiredCurvature = 0.
  h.tick()
  h.tick(1.)
  assert h.params.get(TRANSACTION) is not None
  h.tick(1.)
  assert h.params.get(TRANSACTION) is None


def test_failed_load_rolls_back_then_waits_for_previous_model_health():
  h = Harness()
  h.begin()
  h.load()
  h.sm.bad.add('modelV2')
  h.tick(61.)
  assert h.params.get(TRANSACTION)['restoring']
  h.load()
  assert h.params.get(ACTIVE) == h.previous
  h.sm.bad.clear()
  h.finish()
  assert h.params.get(STATUS) == 'restored'
  assert h.params.get(TRANSACTION) is None
  assert h.params.get_bool(REENGAGE)


def test_failed_rollback_leaves_persistent_lock():
  h = Harness()
  h.begin()
  h.load()
  h.sm.bad.add('modelV2')
  h.tick(61.)
  h.load()
  h.tick(61.)
  assert h.params.get(TRANSACTION)['phase'] == 'failed'
  assert h.params.get(STATUS) == 'failed'
  h.sm.bad.clear()
  h.finish()
  assert h.params.get(TRANSACTION) is not None


def test_manager_restart_restores_previous_bundle_under_lock():
  h = Harness()
  h.begin()
  h.load()
  token = h.params.get(TRANSACTION)['id']
  h.coordinator = ModelSwitchCoordinatorBP(h.params, h.processes, lambda: h.now)
  assert h.params.get(TRANSACTION)['id'] == token
  assert h.params.get(TRANSACTION)['restoring']
  h.load()
  assert h.params.get(ACTIVE) == h.previous
  h.finish()
  assert h.params.get(STATUS) == 'restored'


@pytest.mark.parametrize('previous', [None, {'ref': 'tinygrad', 'runner': 'tinygrad'}])
def test_switch_to_and_from_default(previous):
  h = Harness()
  h.params.put(ACTIVE, previous)
  h.begin({} if previous else h.candidate)
  h.load()
  h.finish()
  assert h.params.get(ACTIVE) == (None if previous else h.candidate)
  assert h.params.get(STATUS) == 'ready'


def test_offroad_selection_does_not_wait_for_nonexistent_onroad_telemetry():
  h = Harness()
  h.sm['deviceState'].started = False
  h.sm.bad.update(SERVICES)
  stage_bundle(h.params, h.candidate)
  h.tick()
  assert h.params.get(ACTIVE) == h.candidate
  assert h.params.get(TRANSACTION) is None
  assert h.params.get(STATUS) == 'selected'


def test_going_offroad_during_load_restores_previous_selection():
  h = Harness()
  h.begin()
  h.load()
  h.sm['deviceState'].started = False
  h.tick()
  assert h.params.get(ACTIVE) == h.previous
  assert h.params.get(TRANSACTION) is None


def test_pending_requests_cannot_overwrite_transaction():
  h = Harness()
  h.begin()
  with pytest.raises(RuntimeError):
    stage_bundle(h.params, {})


def test_unknown_runner_is_rejected_before_activation():
  h = Harness()
  with pytest.raises(ValueError):
    stage_bundle(h.params, {'runner': 'snpe'})
  assert h.params.get(PENDING) is None


def test_engagement_guard_survives_lock_and_requires_fresh_button_press():
  params = Params()
  guard = ModelSwitchEngagementBP(params)
  events = set()
  cs = car.CarState.new_message(vEgo=30., gearShifter='drive')
  params.put(TRANSACTION, {'id': 'switch-1'})
  cs.buttonEvents = [{'type': 'resumeCruise', 'pressed': True}]
  guard.update(cs, events)
  assert log.OnroadEvent.EventName.bpModelSwitch in events
  assert guard.token == 'switch-1'
  params.put_bool(REENGAGE, True)
  params.remove(TRANSACTION)
  cs.buttonEvents = []
  events.clear()
  guard.update(cs, events)
  assert log.OnroadEvent.EventName.bpModelSwitchReady in events
  cs.buttonEvents = [{'type': 'resumeCruise', 'pressed': False}]
  events.clear()
  guard.update(cs, events)
  assert log.OnroadEvent.EventName.bpModelSwitchReady in events
  cs.buttonEvents = [{'type': 'resumeCruise', 'pressed': True}]
  events.clear()
  guard.update(cs, events)
  assert not events
  assert not params.get_bool(REENGAGE)


@pytest.mark.parametrize('button', ['setCruise', 'resumeCruise', 'lkas'])
def test_selfdrived_restart_preserves_manual_reengagement_requirement(button):
  params = Params()
  params.put_bool(REENGAGE, True)
  guard = ModelSwitchEngagementBP(params)
  cs = car.CarState.new_message(vEgo=30.)
  events = set()
  guard.update(cs, events)
  assert log.OnroadEvent.EventName.bpModelSwitchReady in events
  cs.buttonEvents = [{'type': button, 'pressed': True}]
  events.clear()
  guard.update(cs, events)
  assert not events


def test_lock_acknowledgement_schema_roundtrip():
  msg = custom.SelfdriveStateSP.new_message(bpModelSwitchToken='transaction-123',
                                          bpModelSwitchCruiseDisengaged=True, bpModelSwitchCarStateMonoTime=123000000)
  with custom.SelfdriveStateSP.from_bytes(msg.to_bytes()) as decoded:
    assert decoded.bpModelSwitchToken == 'transaction-123'
    assert decoded.bpModelSwitchCruiseDisengaged
    assert decoded.bpModelSwitchCarStateMonoTime == 123000000


@pytest.mark.parametrize('fault', ['missing', 'invalid', 'can_invalid', 'can_timeout', 'cruise_enabled'])
def test_vehicle_relay_fails_closed_and_clears_previous_disengaged_sample(fault):
  guard = ModelSwitchEngagementBP(Params())
  msg = log.Event.new_message(logMonoTime=1000000000, valid=True)
  msg.init('carState')
  msg.carState.canValid = True
  msg.carState.vEgo = 30.
  guard.observe_car_state(msg)
  state = custom.SelfdriveStateSP.new_message()
  guard.populate_state(state)
  assert state.bpModelSwitchCruiseDisengaged
  assert state.bpModelSwitchCarStateMonoTime == msg.logMonoTime
  if fault == 'invalid':
    msg.valid = False
  elif fault == 'can_invalid':
    msg.carState.canValid = False
  elif fault == 'can_timeout':
    msg.carState.canTimeout = True
  elif fault == 'cruise_enabled':
    msg.carState.cruiseState.enabled = True
  guard.observe_car_state(None if fault == 'missing' else msg)
  guard.populate_state(state)
  assert not state.bpModelSwitchCruiseDisengaged
  if fault != 'cruise_enabled':
    assert state.bpModelSwitchCarStateMonoTime == 0


@pytest.mark.parametrize('phase', ['before_lock', 'locking', 'loading'])
@pytest.mark.parametrize('fault', ['missing', 'stale', 'future', 'invalid', 'before_ack'])
def test_fresh_relay_cannot_hide_unusable_vehicle_sample(phase, fault):
  h = Harness()
  if phase == 'before_lock':
    stage_bundle(h.params, h.candidate)
  else:
    h.begin()
    if phase == 'loading':
      h.load()
    else:
      h.tick()  # receive the first acknowledgement
  stops = {n: list(p.stops) for n, p in h.processes.items()}

  def corrupt_source():
    state = h.sm['selfdriveStateSP']
    if fault == 'invalid':
      state.bpModelSwitchCruiseDisengaged = False
    else:
      times = {'missing': 0, 'stale': h.now - 2, 'future': h.now + 1,
               'before_ack': h.coordinator.acknowledged_at or 0}
      state.bpModelSwitchCarStateMonoTime = int(times[fault] * 1e9)

  for _ in range(6):
    h.tick(before=corrupt_source)
  assert {n: p.stops for n, p in h.processes.items()} == stops
  transaction = h.params.get(TRANSACTION)
  assert transaction is None if phase == 'before_lock' else transaction['phase'] == phase
  assert h.params.get(STATUS) != 'ready'
