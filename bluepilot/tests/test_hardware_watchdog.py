"""A blocked publisher must remain observable from the supervising thread."""
import threading

from openpilot.bluepilot.system.hardware_watchdog import HardwareHeartbeatBP


def test_captures_blocked_thread_once_per_heartbeat():
  reports = []
  now = [100.]
  watchdog = HardwareHeartbeatBP(lambda *args, **kwargs: reports.append((args, kwargs)), clock=lambda: now[0])
  blocked = threading.Event()
  ready = threading.Event()

  def publisher():
    watchdog.published()
    ready.set()
    blocked.wait()

  thread = threading.Thread(target=publisher)
  thread.start()
  try:
    assert ready.wait(1)
    watchdog.check(thread.ident)
    assert not reports
    now[0] += 2.1
    watchdog.check(thread.ident)
    assert len(reports) == 1
    args, report = reports[0]
    assert args == ('hardwaredHeartbeatStall',)
    assert report['error']
    assert 'blocked.wait()' in report['threadStacks'][str(thread.ident)]
    watchdog.check(thread.ident)
    assert len(reports) == 1
    watchdog.published()
    now[0] += 2.1
    watchdog.check(thread.ident)
    assert len(reports) == 2
  finally:
    blocked.set()
    thread.join(1)
  assert not thread.is_alive()


def test_no_report_before_first_heartbeat_or_for_missing_thread():
  reports = []
  watchdog = HardwareHeartbeatBP(lambda *args, **kwargs: reports.append(kwargs), clock=lambda: 100.)
  watchdog.check(-1)
  watchdog.last_publish = 90.
  watchdog.check(-1)
  assert not reports
