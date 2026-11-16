"""Keep slow sysfs temperature reads outside hardwared's heartbeat publisher."""
import threading
import time


class ThermalSamplerBP:
  MAX_AGE = 2.0

  def __init__(self, read, end_event, report, clock=time.monotonic, interval=0.5):
    self.read = read
    self.end_event = end_event
    self.report = report
    self.clock = clock
    self.interval = interval
    self.sample = None
    # One worker only: a stuck kernel read must not accumulate replacement threads.
    self.thread = threading.Thread(target=self._run, name="bp_thermal", daemon=True)
    self.thread.start()

  def _run(self):
    failed = False
    while not self.end_event.is_set():
      started = self.clock()
      try:
        temperatures = self.read()
      except Exception as exc:
        if not failed:
          self.report("thermalReadError", error=True, exception=repr(exc))
        failed = True
      else:
        # Timestamp the beginning: a slow partial sweep is already old on return.
        # Publish a complete snapshot with one assignment, never a partial dict.
        self.sample = (started, temperatures)
        failed = False
      if self.end_event.wait(self.interval):
        break

  def snapshot(self):
    """Return the last complete reading and whether thermal safety must fail closed."""
    sample = self.sample
    if sample is None:
      return {}, True
    timestamp, temperatures = sample
    return temperatures, self.clock() - timestamp >= self.MAX_AGE
