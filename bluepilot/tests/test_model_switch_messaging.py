"""Native msgq fan-out regression; isolated from the device's live sockets/Params.

The 2026-09-23 failing route had these 15 carState consumers before manager
subscribed. Exercise their reader slots with the production manager SubMaster,
real serialized messages, and real stock/MADS engagement state machines.
Inference and process restarts are simulated; this is not a vehicle road test.
"""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace

import pytest


CAR_STATE_READERS = (
  'loggerd', 'ui', 'controlsd', 'selfdrived', 'calibrationd', 'locationd',
  'paramsd', 'lagd', 'torqued', 'dmonitoringd', 'plannerd', 'radard',
  'modeld', 'locationd_llk', 'feedbackd',
)


@pytest.mark.parametrize('scenario', ['legacy_overflow', 'stock_to_tinygrad', 'tinygrad_to_stock'])
def test_native_onroad_fanout_and_handover(scenario):
  # ZMQ has different subscriber limits. Force native msgq in a fresh interpreter.
  env = dict(os.environ)
  env.pop('ZMQ', None)
  env.pop('SIMULATION', None)
  env.pop('OPENPILOT_PREFIX', None)
  base = '/tmp' if sys.platform == 'darwin' else '/dev/shm'
  with tempfile.TemporaryDirectory(prefix='msgq_bp_handover_', dir=base) as directory:
    env['OPENPILOT_PREFIX'] = Path(directory).name.removeprefix('msgq_')
    result = subprocess.run([sys.executable, str(Path(__file__).resolve()), scenario], env=env,
                            capture_output=True, text=True, timeout=30)
  assert result.returncode == 0, result.stdout + result.stderr


def run_scenario(scenario):
  import cereal.messaging as messaging
  from cereal import custom, log
  from openpilot.bluepilot.models.engagement import ModelSwitchEngagementBP
  from openpilot.bluepilot.models.switch import ACTIVE, PROCESSES, STATUS, TRANSACTION, manager_submaster, stage_bundle
  from openpilot.bluepilot.tests.test_model_switch import Harness
  from openpilot.selfdrive.selfdrived.events import Events
  from openpilot.selfdrive.selfdrived.state import StateMachine
  from openpilot.sunnypilot.mads.state import StateMachine as MadsStateMachine
  from openpilot.sunnypilot.selfdrive.selfdrived.events import EventsSP

  h = Harness()
  h.previous = {'ref': 'previous', 'runner': 'stock' if scenario == 'stock_to_tinygrad' else 'tinygrad'}
  h.candidate = {'ref': 'candidate', 'runner': 'tinygrad' if scenario == 'stock_to_tinygrad' else 'stock'}
  h.params.put(ACTIVE, h.previous)
  h.ensure_running()
  guard = ModelSwitchEngagementBP(h.params)
  events, events_sp = Events(), EventsSP()
  machine = StateMachine()
  selfdrive = SimpleNamespace(events=events, events_sp=events_sp, enabled=False, state_machine=machine)
  mads = MadsStateMachine(SimpleNamespace(selfdrive=selfdrive))
  services = ['carState', 'deviceState', 'selfdriveState', 'selfdriveStateSP', 'carControl',
              'modelV2', 'longitudinalPlan', 'controlsState']
  pm = messaging.PubMaster(services)
  readers = {name: messaging.sub_sock('carState', conflate=True) for name in CAR_STATE_READERS}
  sm = manager_submaster()
  legacy = messaging.sub_sock('carState', conflate=True) if scenario == 'legacy_overflow' else None
  counts = dict.fromkeys(readers, 0)
  enabled_at = {}
  phases = set()
  loading_started = None

  def message(name):
    return messaging.new_message(name, valid=True, logMonoTime=int(h.now * 1e9))

  # Deterministic 100 Hz car/control, 20 Hz model/plan, 2 Hz manager timing.
  # Only the clock is virtual: sockets, serialization and receive checks are real.
  with pytest.MonkeyPatch.context() as monkeypatch:
    monkeypatch.setattr(messaging.time, 'monotonic', lambda: h.now)
    for frame in range(1000):
      h.now = 100. + frame / 100.
      cs = message('carState')
      cs.carState.canValid = True
      cs.carState.vEgo = 30.
      cs.carState.gearShifter = 'drive'
      cs.carState.cruiseState.enabled = selfdrive.enabled
      if frame in (50, 350, 400, 850):
        cs.carState.buttonEvents = [{'type': 'resumeCruise', 'pressed': True}]
      elif frame in (51, 351, 401, 851):
        cs.carState.buttonEvents = [{'type': 'resumeCruise', 'pressed': False}]
      pm.send('carState', cs)
      received = None
      for name, sock in readers.items():
        sample = messaging.recv_one_or_none(sock)
        if sample is not None:
          counts[name] += 1
          assert sample.valid and sample.carState.canValid
        if name == 'selfdrived':
          received = sample
      if legacy is not None:
        legacy.receive(non_blocking=True)
        continue
      assert received is not None, f'carState reader evicted at frame {frame}'
      guard.observe_car_state(received)
      events.clear()
      events_sp.clear()
      if frame in (50, 350, 400, 850):
        events.add(log.OnroadEvent.EventName.buttonEnable)
        events_sp.add(custom.OnroadEventSP.EventName.lkasEnable)
      if frame == 150:
        events.add(log.OnroadEvent.EventName.buttonCancel)
      guard.update(received.carState, events)
      selfdrive.enabled, active = machine.update(events)
      mads_enabled, mads_active = mads.update()
      enabled_at[frame] = (selfdrive.enabled, mads_enabled)

      ss = message('selfdriveState')
      ss.selfdriveState.enabled = selfdrive.enabled
      pm.send('selfdriveState', ss)
      sp = message('selfdriveStateSP')
      guard.populate_state(sp.selfdriveStateSP)
      sp.selfdriveStateSP.mads.enabled = mads_enabled
      pm.send('selfdriveStateSP', sp)
      cc = message('carControl')
      cc.carControl.enabled = selfdrive.enabled
      cc.carControl.latActive = active or mads_active
      cc.carControl.longActive = active
      pm.send('carControl', cc)
      controls = message('controlsState')
      controls.controlsState.lateralPlanMonoTime = int(h.now * 1e9)
      controls.controlsState.longitudinalPlanMonoTime = int(h.now * 1e9)
      pm.send('controlsState', controls)
      # Allow a model-loading gap to exercise freshness and recovery of checks.
      if frame % 5 == 0 and (loading_started is None or frame - loading_started >= 50):
        pm.send('modelV2', message('modelV2'))
        plan = message('longitudinalPlan')
        plan.longitudinalPlan.modelMonoTime = int(h.now * 1e9)
        pm.send('longitudinalPlan', plan)
      if frame == 250:
        stage_bundle(h.params, h.candidate)
      if frame % 50 == 0:
        device = message('deviceState')
        device.deviceState.started = True
        pm.send('deviceState', device)
        sm.update(0)
        h.coordinator.update(sm)
        h.ensure_running()
        txn = h.params.get(TRANSACTION)
        if txn:
          phases.add(txn['phase'])
          if txn['phase'] == 'loading' and loading_started is None:
            loading_started = frame

  if legacy is not None:
    assert sum(counts.values()) == 0, counts
  else:
    assert set(counts.values()) == {1000}, counts
    assert phases == {'locking', 'loading'}, phases
    assert enabled_at[50] == (True, True)  # ordinary engagement works before selection
    assert enabled_at[150] == (False, False)
    assert enabled_at[350] == enabled_at[400] == (False, False)  # lock blocks both
    assert enabled_at[800] == (False, False)  # never automatically resume
    assert enabled_at[850] == (True, True)  # fresh manual engagement after ready
    assert h.params.get(STATUS) == 'ready'
    assert h.params.get(ACTIVE) == h.candidate
    assert h.params.get(TRANSACTION) is None
    assert all(h.processes[n].stops == [False, True] for n in PROCESSES)
    assert all(not h.processes[n].stops for n in ('card', 'pandad', 'controlsd', 'selfdrived'))
  print(scenario, counts)


if __name__ == '__main__':
  run_scenario(sys.argv[1])
