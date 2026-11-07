"""Manager-owned, offline model handover with a persistent engagement interlock.

Never stop card, controlsd, selfdrived or pandad. Network and SHA verification
belong to the offroad cache worker, not the manager or the UI thread.
"""
import math
import signal
import time
import uuid

PENDING = "BPModelSwitchPending"
TRANSACTION = "BPModelSwitchTransaction"
STATUS = "BPModelSwitchStatus"
ERROR = "BPModelSwitchError"
RETRY = "BPModelSwitchRetry"
REENGAGE = "BPModelSwitchReengage"
ACTIVE_KEYS = {"qcom": "ModelManager_ActiveBundle", "chestnut": "ModelManager_ActiveBundleChestnut"}
PROCESSES = ("modeld", "modeld_tinygrad", "plannerd")
# Do not add a carState subscriber: the native onroad graph can exhaust reader slots.
CONTROL_SERVICES = ("selfdriveState", "selfdriveStateSP", "carControl")
OUTPUT_SERVICES = ("modelV2", "longitudinalPlan", "controlsState")
SERVICES = (*CONTROL_SERVICES, *OUTPUT_SERVICES)
STATUS_TEXT = {
  "waiting": "Disengage steering and cruise control to switch models.",
  "locking": "Preparing model switch. Drive manually.",
  "stopping": "Stopping the previous model. Drive manually.",
  "loading": "Loading model. Drive manually.",
  "rollback": "Restoring the previous model. Drive manually.",
  "failed": "Recovery failed. Drive manually; retry the previous model from Models.",
  "ready": "Model ready. Press SET/RESUME or LKAS to engage.",
  "restored": "Previous model restored. Press SET/RESUME or LKAS to engage.",
  "selected": "Model selected for the next drive.",
  "cancelled": "Switch cancelled. The current model is unchanged.",
}


def manager_submaster():
  import openpilot.cereal.messaging as messaging
  return messaging.SubMaster(['deviceState', 'carParams', 'pandaStates', *SERVICES], poll='deviceState')


def switch_busy(params):
  return any(params.get(key) is not None for key in (PENDING, TRANSACTION, "ModelManager_DownloadRef"))


def stage_bundle(params, bundle, source):
  """Only accept verified, pinned local Favorites; no onroad downloads."""
  from openpilot.bluepilot.models.cache import ready_bundle
  if source not in ACTIVE_KEYS or switch_busy(params):
    raise ValueError("A model operation is already pending or the hardware is invalid")
  if not ready_bundle(params, bundle, source):
    raise ValueError("Favorite is not ready offline; cache it offroad first")
  params.remove(ERROR)
  params.put(STATUS, "waiting", block=True)
  params.put(PENDING, {"bundle": bundle, "source": source, "requested": time.monotonic()}, block=True)


def stop_for_switch(params):
  transaction = params.get(TRANSACTION)
  return transaction is not None and transaction.get('suppress_models', False)


class ModelSwitchCoordinatorBP:
  LOCK_TIMEOUT = 5.0
  STOP_TIMEOUT = 7.0
  KILL_AFTER = 3.0
  LOAD_TIMEOUT = 30.0
  HEALTHY_SECONDS = 2.0

  def __init__(self, params, processes, clock=time.monotonic, cache_ready=None):
    self.params, self.processes, self.clock = params, processes, clock
    if cache_ready is None:
      from openpilot.bluepilot.models.cache import ready_bundle
      cache_ready = ready_bundle
    self.cache_ready = cache_ready
    self.transaction = params.get(TRANSACTION)
    self.healthy_since = self.acknowledged_at = None
    self.started_at = self.clock()
    self.deadline = self.started_at + self.LOCK_TIMEOUT
    if self.transaction is not None:
      # bp-exp used a single small-model slot and no source field. Recover its
      # transaction too, falling back to the shipped model for obsolete manifests.
      if 'source' not in self.transaction:
        from openpilot.sunnypilot.models.helpers import is_bundle_version_compatible
        self.transaction['source'] = 'qcom'
        previous = self.transaction.get('previous')
        if previous and not is_bundle_version_compatible(previous):
          self.transaction['previous'] = None
      # The model process may have died or loaded either bundle before manager died.
      self.transaction.update(phase="locking", restoring=True)
      self._save()
      self._status("rollback")

  def _save(self):
    self.params.put(TRANSACTION, self.transaction, block=True)

  def _status(self, value):
    if self.params.get(STATUS) != value:
      self.params.put(STATUS, value, block=True)

  def _phase(self, phase, timeout):
    self.transaction['phase'] = phase
    if phase in ('stopping', 'loading'):
      self.transaction['suppress_models'] = phase == 'stopping'
    self.started_at = self.clock()
    self.deadline = self.started_at + timeout
    self.healthy_since = None
    self._save()

  def _set_bundle(self, bundle):
    key = ACTIVE_KEYS[self.transaction['source']]
    if bundle:
      self.params.put(key, bundle, block=True)
    else:
      self.params.remove(key)
    self.params.remove("ModelRunnerTypeCache")

  def _target(self):
    return self.transaction['previous' if self.transaction['restoring'] else 'candidate']

  def _fresh(self, sm, services, after=0.0):
    now = self.clock()
    return sm.all_checks(list(services)) and all(
      after < sm.logMonoTime[s] / 1e9 <= now and now - sm.logMonoTime[s] / 1e9 < 1.0 for s in services)

  def _inactive(self, sm, after=0.0):
    cc, state = sm['carControl'], sm['selfdriveStateSP']
    sample_time = state.bpModelSwitchCarStateMonoTime / 1e9
    return (state.bpModelSwitchCruiseDisengaged and
            after < sample_time <= sm.logMonoTime['selfdriveStateSP'] / 1e9 <= self.clock() and
            self.clock() - sample_time < 1.0 and
            not (sm['selfdriveState'].enabled or state.mads.enabled or cc.enabled or cc.latActive or cc.longActive))

  def _running_target(self):
    runner = self.params.get('ModelRunnerTypeCache')
    if runner not in (1, 2):  # tinygrad, stock
      return False
    expected = 'modeld_tinygrad' if runner == 1 else 'modeld'
    return all((p.proc is not None and p.proc.is_alive()) == (name in (expected, 'plannerd'))
               for name in PROCESSES for p in (self.processes[name],))

  def _begin_stop(self):
    # Persist the stop phase BEFORE signaling: ensure_running must not respawn it.
    self._phase('stopping', self.STOP_TIMEOUT)
    self.killed = False
    self._status('rollback' if self.transaction['restoring'] else 'stopping')
    for name in PROCESSES:
      self.processes[name].stop(block=False)

  def _drain(self):
    # Never call stop(block=True): its SIGKILL path contains an unbounded join().
    alive = [self.processes[n] for n in PROCESSES
             if self.processes[n].proc is not None and self.processes[n].proc.is_alive()]
    if alive:
      if self.clock() - self.started_at >= self.KILL_AFTER and not self.killed:
        for proc in alive:
          proc.signal(signal.SIGKILL)
        self.killed = True
      if self.clock() >= self.deadline:
        self._fail('Model process did not exit after SIGKILL; assistance remains locked.')
      return
    for name in PROCESSES:
      self.processes[name].stop(block=False)  # reap only processes already dead
    if not self.transaction['restoring'] and not self.cache_ready(
        self.params, self._target(), self.transaction['source']):
      self._recover('Cached model files changed before activation.')
      return
    self._set_bundle(self._target())
    self._phase('loading', self.LOAD_TIMEOUT)
    self._status('rollback' if self.transaction['restoring'] else 'loading')

  def _finish(self, status):
    self.params.put_bool(REENGAGE, True, block=True)
    self.params.remove(PENDING)
    self.params.remove(RETRY)
    self._status(status)
    self.params.remove(TRANSACTION)
    self.transaction = None

  def _fail(self, reason):
    self.params.put(ERROR, reason, block=True)
    self.transaction['phase'] = 'failed'
    self._save()
    self._status('failed')

  def _recover(self, reason):
    self.params.put(ERROR, reason, block=True)
    if self.transaction['restoring']:
      self._fail(reason)
    else:
      self.transaction['restoring'] = True
      # Keep the original acknowledgement: the same persistent lock is still held.
      self._begin_stop()

  def update(self, sm):
    """Called before ensure_running. Every wait has a wall-clock deadline."""
    try:
      self._update(sm)
    except Exception as exc:
      if self.transaction is None:
        raise
      from openpilot.common.swaglog import cloudlog
      cloudlog.exception("BluePilot model switch failed")
      self._recover(f'{type(exc).__name__}: {exc}')

  def _update(self, sm):
    now = self.clock()
    device_fresh = self._fresh(sm, ('deviceState',))
    started = sm['deviceState'].started
    if self.transaction is None:
      pending = self.params.get(PENDING)
      if pending is None:
        return
      # Requests expire rather than unexpectedly applying after later disengagement.
      if now - pending['requested'] > self.LOCK_TIMEOUT or not device_fresh:
        self.params.remove(PENDING)
        self._status('cancelled')
        return
      if not self.cache_ready(self.params, pending['bundle'], pending['source']):
        self.params.remove(PENDING)
        self.params.put(ERROR, 'Favorite is no longer cached.', block=True)
        self._status('cancelled')
        return
      if started and (not self._fresh(sm, CONTROL_SERVICES) or not self._inactive(sm)):
        self._status('waiting')
        return
      source = pending['source']
      if started and (source == 'chestnut') != sm['deviceState'].chestnutPresent:
        self.params.remove(PENDING)
        self._status('cancelled')
        return
      self.transaction = {
        'id': uuid.uuid4().hex, 'previous': self.params.get(ACTIVE_KEYS[source]), 'candidate': pending['bundle'],
        'source': source, 'phase': 'locking', 'restoring': False,
      }
      self._phase('locking', self.LOCK_TIMEOUT)
      self.acknowledged_at = None
      self.params.remove(PENDING)
      self._status('locking')
      return

    phase = self.transaction['phase']
    if phase == 'failed':
      # Explicit recovery retries are safe even while moving: interlock never clears.
      if self.params.get_bool(RETRY) or (device_fresh and not started):
        self.params.remove(RETRY)
        self.transaction['restoring'] = True
        self.acknowledged_at = None
        self._phase('locking', self.LOCK_TIMEOUT)
        self._status('rollback')
      return

    if device_fresh and not started:
      # Offroad: still drain the old graph before changing a bundle. A switch
      # interrupted by ignition-off always returns to the previous selection.
      self.transaction['restoring'] = True
      if phase != 'stopping':
        self._begin_stop()
      self._drain()
      if self.transaction is not None and self.transaction['phase'] == 'loading':
        self._finish('restored')
      return

    matching = sm['selfdriveStateSP'].bpModelSwitchToken == self.transaction['id']
    if self.acknowledged_at is None and matching and self._fresh(sm, ('selfdriveStateSP',), self.started_at):
      self.acknowledged_at = sm.logMonoTime['selfdriveStateSP'] / 1e9
    acknowledged = (matching and self.acknowledged_at is not None and self._fresh(sm, CONTROL_SERVICES, self.acknowledged_at)
                    and self._inactive(sm, self.acknowledged_at))
    if phase == 'locking':
      if device_fresh and acknowledged:
        self._begin_stop()
      elif now >= self.deadline:
        # No graph change occurred: abandon a new request without requiring reboot.
        if not self.transaction['restoring']:
          self._finish('cancelled')
        else:
          self._fail('Timed out waiting for the engagement interlock acknowledgement.')
      return
    if phase == 'stopping':
      self._drain()
      return

    target = self._target()
    healthy = (device_fresh and acknowledged and self._running_target() and
               self.params.get(ACTIVE_KEYS[self.transaction['source']]) == (target or None) and
               self._fresh(sm, OUTPUT_SERVICES, self.started_at) and
               sm['longitudinalPlan'].modelMonoTime / 1e9 > self.started_at and
               sm['controlsState'].lateralPlanMonoTime / 1e9 > self.started_at and
               sm['controlsState'].longitudinalPlanMonoTime / 1e9 > self.started_at and
               sm['modelV2'].big == (self.transaction['source'] == 'chestnut') and
               math.isfinite(sm['modelV2'].action.desiredCurvature) and math.isfinite(sm['longitudinalPlan'].aTarget))
    if healthy:
      if self.healthy_since is None:
        self.healthy_since = now
      if now - self.healthy_since >= self.HEALTHY_SECONDS:
        self._finish('restored' if self.transaction['restoring'] else 'ready')
        return
    else:
      self.healthy_since = None
    if now >= self.deadline:
      self._recover('Timed out waiting for fresh model, planner and control output.')
