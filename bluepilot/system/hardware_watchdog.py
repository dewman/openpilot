"""Capture hardwared's Python thread stacks when its heartbeat stops."""
import sys
import time
import traceback


class HardwareHeartbeatBP:
  def __init__(self, report, clock=time.monotonic):
    self.report = report
    self.clock = clock
    self.last_publish = None
    self.reported_publish = None

  def published(self):
    self.last_publish = self.clock()

  def check(self, publisher_thread):
    """Called by hardwared's existing supervisory loop, outside the publisher.

    Observe only: no process restarts, altered heartbeat, or changes to safety
    checks. A ctypes call that blocks retains the Python call site in its stack.
    """
    last_publish = self.last_publish
    if last_publish is None or self.clock() - last_publish < 2.0 or self.reported_publish == last_publish:
      return
    frames = sys._current_frames()
    if publisher_thread not in frames:
      return
    self.reported_publish = last_publish
    self.report('hardwaredHeartbeatStall', error=True, seconds=self.clock() - last_publish,
                publisherThread=publisher_thread,
                threadStacks={str(tid): ''.join(traceback.format_stack(frame)) for tid, frame in frames.items()})
