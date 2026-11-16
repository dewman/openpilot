import ctypes
import errno
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

from openpilot.bluepilot.selfdrive.car import controller_telemetry as worker_module
from openpilot.bluepilot.selfdrive.car.bp_card_publisher import capture_controller_state_bp


def car(value=0.25):
  return SimpleNamespace(CC=SimpleNamespace(lateralUncertainty=value),
                         CP=SimpleNamespace(carFingerprint='FORD_F_150_MK14', fingerprintSource='fixed'), init=Mock())


def hold_interpreter_lock(receiver, end_event, fingerprint_forced, fingerprint, parent_pid=None):
  # Real spawned process, deliberately hold its GIL in a native call. A Python
  # Event.wait mock would release the GIL and miss the native messaging hazard.
  receiver.send(b'ready')
  ctypes.PyDLL(None).sleep(10)


def test_snapshot_is_detached_from_mutable_controller():
  ci = car()
  snapshot = capture_controller_state_bp(ci)
  ci.CC.lateralUncertainty = 0.9
  ci.CP.carFingerprint = 'changed'
  assert snapshot.lateralUncertainty == 0.25
  assert snapshot.fingerprint == 'FORD_F_150_MK14'


def test_stalled_process_and_full_socket_do_not_block_controls_ready(monkeypatch, tmp_path):
  from openpilot.selfdrive.car.card import Car
  from openpilot.common.params import Params
  monkeypatch.setattr(worker_module, 'run_telemetry', hold_interpreter_lock)
  telemetry = worker_module.ControllerTelemetryBP()
  ci = car()
  actual_params = Params(str(tmp_path))
  params = SimpleNamespace(put_bool=Mock(wraps=actual_params.put_bool))
  control = SimpleNamespace(initialized_prev=False, CI=ci, CP=ci.CP, CP_SP=None,
                            can_callbacks=(), params=params, sm=SimpleNamespace(all_alive=lambda _: False),
                            bp_telemetry=telemetry, pm=Mock())
  telemetry.start(ci.CP)
  try:
    telemetry.sender.settimeout(5)
    assert telemetry.sender.recv(20) == b'ready'
    telemetry.sender.setblocking(False)
    # Saturate the bounded handoff while the child is unable to consume anything.
    full = False
    for _ in range(10000):
      try:
        telemetry.sender.send(b'x' * worker_module.PACKET.size)
      except OSError as exc:
        assert exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK, errno.ENOBUFS)
        full = True
        break
    assert full
    before = time.monotonic()
    for i in range(1000):
      ci.CC.lateralUncertainty = i
      Car.controls_update(control, None, None, None)
      control.initialized_prev = True
    assert time.monotonic() - before < 1
    control.params.put_bool.assert_called_once_with('ControlsReady', True)
    deadline = time.monotonic() + 1
    while not actual_params.get_bool('ControlsReady') and time.monotonic() < deadline:
      time.sleep(0.005)
    assert actual_params.get_bool('ControlsReady')
    ci.init.assert_called_once()
    control.pm.send.assert_not_called()
    assert telemetry.process.is_alive()
  finally:
    before = time.monotonic()
    telemetry.close()
    assert time.monotonic() - before < 2
  assert not telemetry.process.is_alive()


def test_handoff_reads_latest_packet_without_backlog():
  telemetry = worker_module.ControllerTelemetryBP()
  try:
    for i in range(3):
      telemetry.submit(car(i))
    sample = worker_module.receive_latest(telemetry.receiver)
    assert sample[1] == 2
    assert worker_module.receive_latest(telemetry.receiver) is None
  finally:
    telemetry.close()


def test_worker_drops_stale_status_and_owns_its_publisher(monkeypatch):
  calls = []
  stop = threading.Event()
  telemetry = worker_module.ControllerTelemetryBP()
  snapshot = capture_controller_state_bp(car())
  telemetry.sender.send(worker_module.PACKET.pack(time.monotonic_ns() - worker_module.MAX_AGE_NS * 2, *snapshot[:8]))
  monkeypatch.setattr(worker_module, 'configure_background_thread_bp', lambda: calls.append('configured'))

  def publisher(topics):
    assert calls == ['configured']
    assert topics == ['controllerStateBP']
    return object()

  class OneIteration:
    def is_set(self):
      return stop.is_set()

    def wait(self, timeout):
      stop.set()

  publish = Mock()
  monkeypatch.setattr(worker_module.messaging, 'PubMaster', publisher)
  monkeypatch.setattr(worker_module, 'publish_controller_snapshot_bp', publish)
  monkeypatch.setattr(worker_module, 'refresh_controller_settings_bp', lambda: calls.append('settings'))
  try:
    worker_module.run_telemetry(telemetry.receiver, OneIteration(), True, 'FORD_F_150_MK14')
    publish.assert_not_called()
    assert calls == ['configured', 'settings']
  finally:
    telemetry.close()


def test_background_policy_is_set_before_affinity(monkeypatch):
  calls = []
  monkeypatch.setattr(worker_module, 'drop_realtime', lambda: calls.append('normal'))
  monkeypatch.setattr(worker_module, 'set_core_affinity', lambda cores: calls.append(cores))
  worker_module.configure_background_thread_bp()
  assert calls == ['normal', [0, 1, 2, 3]]


def test_settings_thread_moves_before_spawning_and_reading(monkeypatch):
  from openpilot.selfdrive.car import card
  calls = []
  stop = threading.Event()

  def spawn(_):
    assert calls == ['configured']
    calls.append('spawned')

  def read(_):
    assert calls == ['configured', 'spawned']
    stop.set()
    return True

  monkeypatch.setattr(card, 'is_bluepilot', lambda: True)
  monkeypatch.setattr(card, 'configure_background_thread_bp', lambda: calls.append('configured'))
  monkeypatch.setattr(card.time, 'sleep', lambda _: None)
  control = SimpleNamespace(params=SimpleNamespace(get_bool=read, get=Mock()), v_cruise_helper=Mock(),
                            bp_telemetry=SimpleNamespace(start=spawn), CP=car().CP)
  card.Car.params_thread(control, stop)
  assert calls == ['configured', 'spawned']


def test_spawned_worker_publishes_real_schema_and_settings():
  from openpilot.cereal import messaging
  from openpilot.common.params import Params
  from openpilot.common.prefix import OpenpilotPrefix

  with OpenpilotPrefix():
    Params().put('LC_PID_gain_UI_curv', 3.0, block=True)
    subscriber = messaging.sub_sock('controllerStateBP', timeout=100)
    telemetry = worker_module.ControllerTelemetryBP()
    ci = car()
    telemetry.start(ci.CP)
    message = None
    try:
      deadline = time.monotonic() + 5
      while message is None and time.monotonic() < deadline:
        telemetry.submit(ci)
        message = messaging.recv_one(subscriber)
      assert message is not None
      assert message.valid
      assert message.controllerStateBP.lateralUncertainty == 0.25
      assert message.controllerStateBP.bmsFingerprint == 'FORD_F_150_MK14'
      assert message.controllerStateBP.bmsFingerprintForced
      assert message.controllerStateBP.bmsCenteringPidGain == 3.0
      assert 0 <= time.monotonic_ns() - message.logMonoTime < 1_000_000_000
    finally:
      telemetry.close()
      del subscriber
    assert not telemetry.process.is_alive()
