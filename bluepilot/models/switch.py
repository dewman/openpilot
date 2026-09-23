"""Manager-owned model handover. No vehicle speed or gear restriction.

Downloads only stage a bundle. Activation requires a selfdrived lock acknowledgement
and fresh, inactive control messages. The persistent transaction is also the
engagement interlock: a manager crash cannot silently unlock assistance.
"""
import math
import time
import uuid


PENDING = "BPModelSwitchPending"
TRANSACTION = "BPModelSwitchTransaction"
STATUS = "BPModelSwitchStatus"
REENGAGE = "BPModelSwitchReengage"
ACTIVE = "ModelManager_ActiveBundle"
PROCESSES = ("modeld", "modeld_tinygrad", "plannerd")
# The onroad graph already uses all 15 native msgq carState reader slots.
# selfdrived relays factory-cruise status using its existing subscription.
CONTROL_SERVICES = ("selfdriveState", "selfdriveStateSP", "carControl")
OUTPUT_SERVICES = ("modelV2", "longitudinalPlan", "controlsState")
SERVICES = (*CONTROL_SERVICES, *OUTPUT_SERVICES)


STATUS_TEXT = {
  "waiting": "Model downloaded. Disengage steering and cruise control to apply.",
  "locking": "Preparing model switch. Drive manually; assistance is unavailable.",
  "loading": "Loading model. Drive manually; assistance is unavailable.",
  "rollback": "Model failed to load. Restoring the previous model; drive manually.",
  "failed": "Model recovery failed. Assistance is unavailable; restart the software when safe.",
  "ready": "Model ready. Press SET/RESUME or LKAS to engage when ready.",
  "restored": "Previous model restored. Press SET/RESUME or LKAS to engage when ready.",
  "selected": "Model selected for the next drive.",
  "download_failed": "Model download failed. The current model is unchanged; check connectivity and retry.",
  "cancelled": "Model download cancelled. The current model is unchanged.",
}


def manager_submaster():
  """Share the production subscription set with native IPC regression tests."""
  import cereal.messaging as messaging
  return messaging.SubMaster(['deviceState', 'carParams', 'pandaStates', *SERVICES], poll='deviceState')


def switch_busy(params):
  return params.get(PENDING) is not None or params.get(TRANSACTION) is not None or params.get("ModelManager_DownloadIndex") is not None


def stage_bundle(params, bundle):
  """Publish only a fully downloaded/verified bundle; an empty bundle means default."""
  if params.get(PENDING) is not None or params.get(TRANSACTION) is not None:
    raise RuntimeError("A model switch is already pending")
  if bundle and bundle.get('runner') not in ('stock', 'tinygrad'):
    raise ValueError("Unsupported model runner")
  params.put(STATUS, "waiting", block=True)
  params.put(PENDING, {"bundle": bundle}, block=True)


def displayed_bundle(params):
  """Do not describe an unproven candidate as the running model."""
  transaction = params.get(TRANSACTION)
  return transaction["previous"] if transaction else params.get(ACTIVE)


class ModelSwitchCoordinatorBP:
  LOCK_TIMEOUT = 10.0
  LOAD_TIMEOUT = 60.0
  HEALTHY_SECONDS = 2.0

  def __init__(self, params, processes, clock=time.monotonic):
    self.params = params
    self.processes = processes
    self.clock = clock
    self.transaction = params.get(TRANSACTION)
    self.healthy_since = None
    self.acknowledged_at = None
    self.started_at = self.clock()
    self.deadline = self.started_at + self.LOCK_TIMEOUT
    if self.transaction is not None:
      # Recover an interrupted handover under the same persistent lock.
      self.transaction.update(phase="locking", restoring=True)
      self._save()
      self._status("rollback")

  def _save(self):
    self.params.put(TRANSACTION, self.transaction, block=True)

  def _status(self, value):
    if self.params.get(STATUS) != value:
      self.params.put(STATUS, value, block=True)

  def _set_bundle(self, bundle):
    if bundle:
      self.params.put(ACTIVE, bundle, block=True)
    else:
      self.params.remove(ACTIVE)
    # Both runner predicates will resolve the new bundle at the next ensure_running.
    self.params.remove("ModelRunnerTypeCache")

  def _stop(self):
    for name in PROCESSES:
      self.processes[name].stop(block=False)
    for name in PROCESSES:
      self.processes[name].stop(block=True)

  def _fresh(self, sm, services, after=0.0):
    now = self.clock()
    return sm.all_checks(list(services)) and all(
      after < sm.logMonoTime[s] / 1e9 <= now and now - sm.logMonoTime[s] / 1e9 < 1.0 for s in services)

  def _inactive(self, sm, after=0.0):
    cc = sm['carControl']
    state = sm['selfdriveStateSP']
    sample_time = state.bpModelSwitchCarStateMonoTime / 1e9
    # A freshly published relay must not disguise a stale/invalid carState sample.
    # After locking, require a source sample newer than the acknowledgement too.
    return (state.bpModelSwitchCruiseDisengaged and
            after < sample_time <= sm.logMonoTime['selfdriveStateSP'] / 1e9 <= self.clock() and
            self.clock() - sample_time < 1.0 and
            not (sm['selfdriveState'].enabled or state.mads.enabled or cc.enabled or cc.latActive or cc.longActive))

  def _running_target(self, target):
    runner = target.get('runner', 'stock') if target else 'stock'
    expected = 'modeld_tinygrad' if runner == 'tinygrad' else 'modeld'
    for name in PROCESSES:
      proc = self.processes[name].proc
      running = proc is not None and proc.is_alive()
      if running != (name in (expected, 'plannerd')):
        return False
    return True

  def _restart(self):
    self._stop()
    target = self.transaction['previous'] if self.transaction['restoring'] else self.transaction['candidate']
    self._set_bundle(target)
    self.transaction['phase'] = "loading"
    self._save()
    self.started_at = self.clock()
    self.deadline = self.started_at + self.LOAD_TIMEOUT
    self.healthy_since = None
    self._status("rollback" if self.transaction['restoring'] else "loading")

  def _finish(self, status):
    # Persist the manual re-engagement requirement before removing the lock.
    self.params.put_bool(REENGAGE, True, block=True)
    self.params.remove(PENDING)
    self._status(status)
    self.params.remove(TRANSACTION)
    self.transaction = None

  def _recover(self):
    if self.transaction['restoring']:
      self.transaction['phase'] = "failed"
      self._save()
      self._status("failed")
    else:
      self.transaction.update(phase="locking", restoring=True)
      self._save()
      self.started_at = self.clock()
      self.acknowledged_at = None
      self.deadline = self.started_at + self.LOCK_TIMEOUT
      self._status("rollback")

  def update(self, sm):
    """Called before manager.ensure_running; never restarts controlsd or pandad."""
    try:
      self._update(sm)
    except Exception:
      # Keep a persistent lock on any unexpected failure, including process/Params
      # errors. The manager's existing exception path handles failed disk writes.
      if self.transaction is None:
        raise
      from openpilot.common.swaglog import cloudlog
      cloudlog.exception("BluePilot model switch failed")
      self._recover()

  def _update(self, sm):
    if not self._fresh(sm, ('deviceState',)):
      self.healthy_since = None
      return

    started = sm['deviceState'].started
    pending = self.params.get(PENDING)
    if self.transaction is None:
      if pending is None:
        return
      if started and (not self._fresh(sm, CONTROL_SERVICES) or not self._inactive(sm)):
        self._status("waiting")
        return
      self.transaction = {
        "id": uuid.uuid4().hex, "previous": self.params.get(ACTIVE), "candidate": pending['bundle'],
        "phase": "locking", "restoring": False,
      }
      self._save()
      self.params.remove(PENDING)
      self.started_at = self.clock()
      self.acknowledged_at = None
      self.deadline = self.started_at + self.LOCK_TIMEOUT
      self._status("locking")
      if started:
        return

    if not started:
      # There is no running driving model to warm up offroad. An interrupted
      # onroad attempt always restores the known previous selection.
      restoring = self.transaction['restoring'] or self.transaction['phase'] != 'locking'
      self._stop()
      self._set_bundle(self.transaction['previous'] if restoring else self.transaction['candidate'])
      self._finish("restored" if restoring else "selected")
      return

    phase = self.transaction['phase']
    if phase == 'failed':
      return

    matching_token = sm['selfdriveStateSP'].bpModelSwitchToken == self.transaction['id']
    if self.acknowledged_at is None and matching_token and self._fresh(sm, ('selfdriveStateSP',), self.started_at):
      self.acknowledged_at = sm.logMonoTime['selfdriveStateSP'] / 1e9
    # Inactive commands must have been produced AFTER selfdrived acknowledged the
    # interlock, not merely after manager requested it. This closes an enable race
    # between separate selfdriveStateSP and carControl subscribers.
    acknowledged = (matching_token and self.acknowledged_at is not None and
                    self._fresh(sm, CONTROL_SERVICES, max(self.started_at, self.acknowledged_at)) and
                    self._inactive(sm, max(self.started_at, self.acknowledged_at)))
    if phase == 'locking':
      if acknowledged:
        self._restart()
      elif self.clock() > self.deadline:
        # Never stop a model without acknowledgement that control is inhibited.
        self.transaction['phase'] = 'failed'
        self._save()
        self._status('failed')
      return

    target = self.transaction['previous'] if self.transaction['restoring'] else self.transaction['candidate']
    healthy = (acknowledged and self._running_target(target) and self.params.get(ACTIVE) == (target or None) and
               self._fresh(sm, OUTPUT_SERVICES, self.started_at) and
               sm['longitudinalPlan'].modelMonoTime / 1e9 > self.started_at and
               sm['controlsState'].lateralPlanMonoTime / 1e9 > self.started_at and
               sm['controlsState'].longitudinalPlanMonoTime / 1e9 > self.started_at and
               math.isfinite(sm['modelV2'].action.desiredCurvature) and math.isfinite(sm['longitudinalPlan'].aTarget))
    if healthy:
      if self.healthy_since is None:
        self.healthy_since = self.clock()
      if self.clock() - self.healthy_since >= self.HEALTHY_SECONDS:
        self._finish("restored" if self.transaction['restoring'] else "ready")
        return
    else:
      self.healthy_since = None
    if self.clock() > self.deadline:
      self._recover()
