"""Best-effort display telemetry in a separate, non-real-time process."""
import multiprocessing
import ctypes
import os
import signal
import socket
import struct
import sys
import time

from openpilot.cereal import messaging
from openpilot.common.realtime import DT_CTRL, drop_realtime, set_core_affinity
from openpilot.common.swaglog import cloudlog
from openpilot.bluepilot.selfdrive.car.bp_card_publisher import (
  ControllerSnapshotBP, capture_controller_state_bp, publish_controller_snapshot_bp, refresh_controller_settings_bp,
)

# Only per-frame scalars cross this handoff, not the full Cereal/settings message.
PACKET = struct.Struct('<Qd6?B')
MAX_AGE_NS = 100_000_000


def configure_background_thread_bp():
  """Apply to the calling thread before I/O or spawning its telemetry child."""
  drop_realtime()
  set_core_affinity([0, 1, 2, 3])


def receive_latest(receiver):
  """Drain a bounded number of datagrams and keep only the newest complete one."""
  latest = None
  for _ in range(64):
    try:
      packet = receiver.recv(PACKET.size + 1)
    except BlockingIOError:
      break
    if len(packet) == PACKET.size:
      latest = PACKET.unpack(packet)
  return latest


def run_telemetry(receiver, end_event, fingerprint_forced, fingerprint, parent_pid=None):
  try:
    configure_background_thread_bp()
    if parent_pid is not None and sys.platform == 'linux':
      # Also clean up if manager has to SIGKILL card, bypassing its finally block.
      libc = ctypes.CDLL(None, use_errno=True)
      libc.prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
      if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0:  # PR_SET_PDEATHSIG
        raise OSError(ctypes.get_errno(), 'setting telemetry parent-death signal')
      if os.getppid() != parent_pid:
        return
    pm = messaging.PubMaster(['controllerStateBP'])
    failed = False
    while not end_event.is_set():
      started = time.monotonic()
      refresh_controller_settings_bp()
      sample = receive_latest(receiver)
      if sample is not None:
        timestamp, *values = sample
        if 0 <= time.monotonic_ns() - timestamp < MAX_AGE_NS:
          try:
            snapshot = ControllerSnapshotBP(*values, fingerprint_forced, fingerprint)
            publish_controller_snapshot_bp(snapshot, pm, timestamp)
          except Exception:
            if not failed:
              cloudlog.exception('BP controller telemetry publish failed')
            failed = True
          else:
            failed = False
      # No stale re-publication or catch-up bursts after a blocked writer.
      end_event.wait(max(0.001, DT_CTRL - (time.monotonic() - started)))
  except Exception:
    cloudlog.exception('BP controller telemetry stopped')
  finally:
    receiver.close()


class ControllerTelemetryBP:
  def __init__(self):
    # Kernel-bounded datagrams; unlike multiprocessing.Queue, no feeder thread or
    # Python lock can block card. Full socket buffers simply drop display samples.
    self.sender, self.receiver = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
    self.sender.setblocking(False)
    self.receiver.setblocking(False)
    self.sender.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 2048)
    self.process = None
    self.end_event = None

  def submit(self, ci):
    snapshot = capture_controller_state_bp(ci)
    if snapshot is None:
      return
    packet = PACKET.pack(time.monotonic_ns(), *snapshot[:8])
    try:
      self.sender.send(packet)
    except OSError:
      # Telemetry is optional: a full buffer or dead consumer cannot stop controls.
      pass

  def start(self, cp):
    # Called by card's already-demoted settings thread. The new interpreter must
    # inherit housekeeping CPUs/normal priority even during its startup imports.
    ctx = multiprocessing.get_context('spawn')
    self.end_event = ctx.Event()
    self.process = ctx.Process(target=run_telemetry,
                               args=(self.receiver, self.end_event,
                                     getattr(cp, 'fingerprintSource', None) == 'fixed',
                                     str(getattr(cp, 'carFingerprint', '') or ''), os.getpid()),
                               name='bp_telemetry', daemon=True)
    self.process.start()
    self.receiver.close()

  def close(self):
    self.sender.close()
    self.receiver.close()
    if self.process is not None and self.process.pid is not None:
      self.end_event.set()
      self.process.join(timeout=0.1)
      if self.process.is_alive():
        self.process.terminate()
        self.process.join(timeout=0.1)
        if self.process.is_alive():
          self.process.kill()
          self.process.join(timeout=0.1)
