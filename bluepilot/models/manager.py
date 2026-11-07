"""Offroad model downloads and Favorites prefetch, layered over SunnyPilot."""
import os
import shutil
import time

from openpilot.cereal import custom
from openpilot.common.hardware.hw import Paths
from openpilot.common.swaglog import cloudlog
from openpilot.sunnypilot.models.manager import ModelManagerSP, DownloadCancelled
from openpilot.sunnypilot.models.helpers import verify_file
from openpilot.bluepilot.models.cache import (
  FAVORITES, CACHE_STATUS, artifacts, favorite_refs, signature, ensure_artifact, pinned_favorites,
)


class ModelManagerBP(ModelManagerSP):
  def __init__(self):
    super().__init__()
    self.prefetching = False
    self.verified = {}
    self.retry_at = {}
    # SIGKILL during ignition transition can leave a private partial download.
    # Only this offroad manager writes these directories; they are never models.
    os.makedirs(Paths.model_root(), exist_ok=True)
    with os.scandir(Paths.model_root()) as entries:
      for entry in entries:
        if entry.name.startswith('.bp-download-') and entry.is_dir(follow_symlinks=False):
          shutil.rmtree(entry.path)

  def _download_interrupted(self):
    if self.params.get_bool('IsOffroad') is False:
      return True
    if self.prefetching:
      return self.params.get('ModelManager_DownloadRef') is not None
    return super()._download_interrupted()

  def _report_status(self):
    if not self.prefetching:
      super()._report_status()

  async def _process_artifact(self, artifact, destination_path):
    if not artifact.fileName:
      return
    status = custom.ModelManagerSP.DownloadStatus
    self._set_progress(artifact, status.verifying, 0)
    downloaded = await ensure_artifact(
      artifact, destination_path, verify_file,
      lambda path: self._download_file(artifact.downloadUri.uri, path, artifact), self._download_interrupted,
    )
    self._set_progress(artifact, status.downloaded if downloaded else status.cached, 100)

  def _pin_favorites(self):
    refs = favorite_refs(self.params)
    pinned = {ref: value for ref, value in pinned_favorites(self.params).items() if ref in refs}
    for source, bundles in self.source_models.items():
      for bundle in bundles:
        if bundle.ref in refs:
          # Favor current compatible metadata over obsolete pinned catalog entries.
          pinned[bundle.ref] = {'source': source, 'bundle': bundle.to_dict()}
    if self.params.get(FAVORITES) != pinned:
      self.params.put(FAVORITES, pinned, block=True)
    return pinned

  def _idle_tasks(self):
    pinned = self._pin_favorites()
    statuses = self.params.get(CACHE_STATUS) or {}
    statuses = {ref: statuses.get(ref, {'state': 'pending'}) for ref in favorite_refs(self.params)}
    directory = Paths.model_root()
    os.makedirs(directory, exist_ok=True)
    for ref, entry in pinned.items():
      bundle = entry['bundle']
      before = signature(bundle, directory)
      if before is not None and self.verified.get(ref) == before:
        statuses[ref] = {'state': 'ready', 'signature': before}
        continue
      statuses[ref] = {'state': 'pending'}
      if time.monotonic() < self.retry_at.get(ref, 0) or self.params.get('ModelManager_DownloadRef') is not None:
        continue
      if not self.params.get_bool('IsOffroad'):
        break
      statuses[ref] = {'state': 'downloading'}
      self.params.put(CACHE_STATUS, statuses, block=True)
      self.prefetching = True
      try:
        # Prefetch does not call _download_bundle: it must never activate a favorite.
        import asyncio
        async def prepare(raw_bundle=bundle):
          for model in custom.ModelManagerSP.ModelBundle(**raw_bundle).models:
            if self._download_interrupted():
              raise DownloadCancelled('Favorite prefetch interrupted')
            await self._process_artifact(model.artifact, directory)
        asyncio.run(prepare())
        after = signature(bundle, directory)
        if after is None:
          raise ValueError('Favorite files missing after verification')
        self.verified[ref] = after
        statuses[ref] = {'state': 'ready', 'signature': after}
      except Exception:
        statuses[ref] = {'state': 'pending' if self._download_interrupted() else 'failed'}
        self.retry_at[ref] = time.monotonic() + 60
        cloudlog.exception('BluePilot Favorite caching failed')
      finally:
        self.prefetching = False
        self.params.put(CACHE_STATUS, statuses, block=True)
    if self.params.get(CACHE_STATUS) != statuses:
      self.params.put(CACHE_STATUS, statuses, block=True)

  def clear_model_cache(self):
    # Pinning Favorites also protects them from the existing Clear Cache action.
    from openpilot.sunnypilot.models.helpers import ACTIVE_BUNDLE_KEYS, get_selected_bundle
    keep = set()
    for source in ACTIVE_BUNDLE_KEYS:
      if bundle := get_selected_bundle(self.params, source):
        keep.update(artifacts(bundle.to_dict()))
    from openpilot.bluepilot.models.warmup import marker_path
    for entry in self._pin_favorites().values():
      keep.update(artifacts(entry['bundle']))
      keep.add(marker_path(entry['bundle'], entry['source'], Paths.model_root()).name)
    for name in os.listdir(Paths.model_root()):
      path = os.path.join(Paths.model_root(), name)
      if os.path.isfile(path) and name not in keep and not any(name.startswith(f'{base}.') and name.endswith('.cache') for base in keep):
        os.unlink(path)
