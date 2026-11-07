"""Extra offroad warmup paths; keep all GPU work out of the onroad selector."""
import os
from openpilot.bluepilot.models.cache import pinned_favorites, ready_bundle
from openpilot.common.hardware.hw import Paths


def favorite_paths(params):
  paths = []
  for entry in pinned_favorites(params).values():
    bundle = entry['bundle']
    if entry['source'] == 'qcom' and ready_bundle(params, bundle, 'qcom'):
      paths.extend(os.path.join(Paths.model_root(), model['artifact']['fileName']) for model in bundle['models'])
  return paths


def run_worker(worker, params, timeout=30):
  """Bound a cold GPU/cache operation, and clean up on ignition or interruption."""
  import time
  worker.start()
  deadline = time.monotonic() + timeout
  try:
    while worker.is_alive() and params.get_bool('IsOffroad') and time.monotonic() < deadline:
      worker.join(timeout=0.2)
  finally:
    if worker.is_alive():
      worker.terminate()
      worker.join(timeout=1)
    if worker.is_alive():
      worker.kill()
      worker.join(timeout=1)
