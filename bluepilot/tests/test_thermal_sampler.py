"""Blocked, failed and recovered thermal reads must not block deviceState."""
import threading

from openpilot.bluepilot.system.thermal_sampler import ThermalSamplerBP


def test_blocked_read_keeps_snapshot_bounded_and_recovers():
  now = [10.]
  end = threading.Event()
  blocked = threading.Event()
  release = threading.Event()
  recovered = threading.Event()
  count = [0]

  def read():
    count[0] += 1
    if count[0] == 2:
      blocked.set()
      release.wait()
    if count[0] >= 3:
      recovered.set()
    return {'cpuTempC': [65.]}

  sampler = ThermalSamplerBP(read, end, lambda *a, **kw: None, clock=lambda: now[0], interval=0.001)
  try:
    assert blocked.wait(1)
    assert sampler.snapshot() == ({'cpuTempC': [65.]}, False)
    now[0] = 12.
    # Calls return while the only worker remains blocked; the age limit is inclusive.
    for _ in range(100):
      assert sampler.snapshot() == ({'cpuTempC': [65.]}, True)
    assert count[0] == 2
    now[0] = 26.
    release.set()
    assert recovered.wait(1)
    # Joining the released worker also waits for complete snapshot publication.
    end.set()
    sampler.thread.join(1)
    assert sampler.snapshot() == ({'cpuTempC': [65.]}, False)
  finally:
    release.set()
    end.set()
    sampler.thread.join(1)
  assert not sampler.thread.is_alive()


def test_missing_and_failed_reads_are_not_fresh():
  end = threading.Event()
  report = threading.Event()
  release = threading.Event()
  reports = []

  def read():
    release.wait()
    raise OSError('sensor unavailable')

  def log(*args, **kwargs):
    reports.append((args, kwargs))
    report.set()

  sampler = ThermalSamplerBP(read, end, log, interval=0.001)
  try:
    assert sampler.snapshot() == ({}, True)
    release.set()
    assert report.wait(1)
    assert sampler.snapshot() == ({}, True)
    assert reports[0][0] == ('thermalReadError',)
  finally:
    end.set()
    release.set()
    sampler.thread.join(1)
  assert not sampler.thread.is_alive()


def test_slow_sweep_timestamp_does_not_make_old_readings_fresh():
  now = [10.]
  end = threading.Event()

  def read():
    now[0] = 25.
    end.set()
    return {'cpuTempC': [99.]}

  sampler = ThermalSamplerBP(read, end, lambda *a, **kw: None, clock=lambda: now[0])
  sampler.thread.join(1)
  assert sampler.snapshot() == ({'cpuTempC': [99.]}, True)
