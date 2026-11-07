"""Prepare the real modeld loader offroad, including both supported camera sizes."""
import functools
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time

from openpilot.bluepilot.models.cache import pinned_favorites, ready_bundle, signature
from openpilot.common.hardware.hw import Paths

CAMERA_SIZES = ((1344, 760), (1928, 1208))


@functools.cache
def preparation_identity():
  """Small source fingerprint, cached once per process; never hashes model weights."""
  # Do not import tinygrad in UI: its import initializes backend settings.
  from openpilot.common.basedir import BASEDIR
  root = Path(BASEDIR)
  digest = hashlib.sha256()
  tree = root / 'tinygrad_repo/tinygrad'
  for directory, _, files in sorted(os.walk(tree)):
    for name in sorted(name for name in files if name.endswith('.py')):
      path = Path(directory) / name
      digest.update(str(path.relative_to(tree)).encode() + path.read_bytes())
  for name in ('helpers.py', 'frame_fusion.py', 'model_adapters.py', 'modeld.py'):
    digest.update((root / 'openpilot/sunnypilot/modeld_v2' / name).read_bytes())
  env = {name: ('0' if name == 'GMMU' else os.environ.get(name, ''))
         for name in ('GMMU', 'AMD_AQL', 'AMD_DISABLE_SDMA', 'GRAPH_ONE_KERNEL')}
  digest.update(json.dumps(env, sort_keys=True).encode())
  return digest.hexdigest()


def marker_path(bundle, source, directory):
  token = hashlib.sha256(f'{source}:{bundle["ref"]}'.encode()).hexdigest()
  return Path(directory) / f'.bp-ready-{token}.json'


def cache_signatures(names, directory):
  result = []
  for name in names:
    if Path(name).name != name:
      raise ValueError('Cache path must stay inside model directory')
    stat = (Path(directory) / name).stat()
    result.append([name, stat.st_size, stat.st_mtime_ns, stat.st_ino])
  return result


def switch_ready_bundle(params, bundle, source, directory=None):
  directory = directory or Paths.model_root()
  if not ready_bundle(params, bundle, source, directory):
    return False
  try:
    marker = json.loads(marker_path(bundle, source, directory).read_text())
    return bool(marker['engine'] == preparation_identity() and
                marker['bundle'] == signature(bundle, directory) and marker['caches'] and
                marker['caches'] == cache_signatures([entry[0] for entry in marker['caches']], directory))
  except (OSError, ValueError, KeyError, TypeError):
    return False


def prepare_camera(bundle, source, width, height):
  """Use the same initialization and dummy inference as modeld, without activation."""
  os.environ['GMMU'] = '0'
  # An externally supplied debug model must never replace the requested Favorite.
  os.environ.pop('COMBINED_MODEL_PKL', None)
  from openpilot.cereal import custom
  from openpilot.sunnypilot.modeld_v2.modeld import ModelState
  if source == 'chestnut':
    from tinygrad.runtime.ops_amd import AMDDevice
    AMDDevice.wait_timeout_ms = 3000
  model = ModelState(width, height, chestnut=source == 'chestnut',
                     model_bundle=custom.ModelManagerSP.ModelBundle(**bundle))
  model.warmup()


def prepared_cache_paths(bundle, source, directory):
  from openpilot.sunnypilot.modeld_v2.frame_fusion import fused_with_warp
  from openpilot.sunnypilot.modeld_v2.helpers import _extended, cache_is_fresh, model_cache_path, read_parts
  path = str(Path(directory) / bundle['models'][0]['artifact']['fileName'])
  native = 'input_specs' in read_parts(path)[1]
  names = []
  for width, height in CAMERA_SIZES if native else CAMERA_SIZES[:1]:
    extend = fused_with_warp(width, height, source == 'chestnut')
    if not cache_is_fresh(path, extend=extend):
      raise RuntimeError(f'Runtime cache not prepared for {width}x{height}')
    extra = _extended(path, extend)[2]
    names.append(Path(model_cache_path(path, extra)).name)
  return names


def publish_ready(bundle, source, directory, before):
  if before is None or before != signature(bundle, directory):
    raise RuntimeError('Model changed while preparing it')
  names = prepared_cache_paths(bundle, source, directory)
  # Keep only the current camera variants; code updates must not accumulate
  # historical GPU-cache files indefinitely.
  base = bundle['models'][0]['artifact']['fileName']
  for old in Path(directory).iterdir():
    if old.name.startswith(f'{base}.') and old.name.endswith('.cache') and old.name not in names:
      old.unlink()
  marker = {'engine': preparation_identity(), 'bundle': before, 'caches': cache_signatures(names, directory)}
  target = marker_path(bundle, source, directory)
  with tempfile.NamedTemporaryFile(mode='w', dir=directory, prefix='.bp-ready-', suffix='.tmp', delete=False) as stream:
    temporary = Path(stream.name)
    json.dump(marker, stream)
  try:
    os.replace(temporary, target)
  finally:
    temporary.unlink(missing_ok=True)


def run_worker(worker, params, timeout=30):
  """Bound cold GPU work; return success only after clean exit while still offroad."""
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
  return worker.exitcode == 0 and params.get_bool('IsOffroad')


def main():
  import multiprocessing
  from openpilot.common.params import Params
  from openpilot.common.swaglog import cloudlog
  from openpilot.selfdrive.modeld.helpers import chestnut_present
  params, spawn, retry_at = Params(), multiprocessing.get_context('spawn'), {}
  directory = Paths.model_root()
  while True:
    entries = pinned_favorites(params)
    for entry in entries.values():
      bundle, source = entry['bundle'], entry['source']
      if (not params.get_bool('IsOffroad') or bundle.get('runner') != 'tinygrad' or
          (source == 'chestnut' and not chestnut_present())):
        continue
      if not ready_bundle(params, bundle, source, directory) or switch_ready_bundle(params, bundle, source, directory):
        continue
      if time.monotonic() < retry_at.get(bundle['ref'], 0):
        continue
      marker_path(bundle, source, directory).unlink(missing_ok=True)
      before = signature(bundle, directory)
      try:
        # One GPU-owning child per camera variant; no retained context or model RAM.
        for width, height in CAMERA_SIZES:
          if not params.get_bool('IsOffroad'):
            raise RuntimeError('Ignition interrupted model preparation')
          worker = spawn.Process(target=prepare_camera, args=(bundle, source, width, height), daemon=True)
          if not run_worker(worker, params):
            raise RuntimeError(f'Model preparation failed for {width}x{height}; exit={worker.exitcode}')
        if not params.get_bool('IsOffroad'):
          raise RuntimeError('Ignition interrupted model preparation')
        publish_ready(bundle, source, directory, before)
      except Exception:
        retry_at[bundle['ref']] = time.monotonic() + 60
        cloudlog.exception('BluePilot Favorite preparation failed')
    time.sleep(1)
