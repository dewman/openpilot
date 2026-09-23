"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""

import asyncio
import os
import time

import aiohttp
from openpilot.common.params import Params
from openpilot.common.realtime import Ratekeeper
from openpilot.common.swaglog import cloudlog
from openpilot.system.hardware.hw import Paths

from cereal import messaging, custom
from openpilot.sunnypilot.models.fetcher import ModelFetcher
from openpilot.sunnypilot.models.helpers import get_active_bundle, validate_active_bundle, verify_file
# BluePilot: downloads stage candidates instead of replacing the live selection.
from openpilot.bluepilot.models.switch import PENDING, TRANSACTION, STATUS, displayed_bundle, stage_bundle
from openpilot.bluepilot.models.favorites import FAVORITES, favorite_refs, include_pinned_favorites
from openpilot.bluepilot.models.cache import ensure_artifact
# End BluePilot


class ModelManagerSP:
  """Manages model downloads and status reporting"""

  # BluePilot: a separate cache worker reuses downloads without publishing/activating.
  def __init__(self, cache_only=False):
    self.params = Params()
    self.model_fetcher = ModelFetcher(self.params)
    self.cache_only = cache_only
    self.pm = None if cache_only else messaging.PubMaster(["modelManagerSP"])
    # End BluePilot
    self.available_models: list[custom.ModelManagerSP.ModelBundle] = []
    self.selected_bundle: custom.ModelManagerSP.ModelBundle = None
    self.active_bundle: custom.ModelManagerSP.ModelBundle = get_active_bundle(self.params)
    self._chunk_size = 128 * 1000  # 128 KB chunks
    self._download_start_times: dict[str, float] = {}  # Track start time per model

  # BluePilot: prefetch yields to driving or foreground selection.
  def _download_cancelled(self):
    if self.cache_only:
      return (self.params.get_bool('IsOnroad') or self.params.get('ModelManager_DownloadIndex') is not None or
              self.params.get(PENDING) is not None or self.params.get(TRANSACTION) is not None or
              self.selected_bundle.ref not in favorite_refs(self.params))
    return self.params.get("ModelManager_DownloadIndex") is None
  # End BluePilot

  def _sync_artifact_progress(self, source_artifact) -> None:
    """Mirror download progress to all artifacts sharing the same filename in the selected bundle."""
    if not self.selected_bundle:
      return
    for model in self.selected_bundle.models:
      for artifact in (model.artifact, model.metadata):
        if artifact is not source_artifact and artifact.fileName == source_artifact.fileName:
          artifact.downloadProgress.status = source_artifact.downloadProgress.status
          artifact.downloadProgress.progress = source_artifact.downloadProgress.progress
          artifact.downloadProgress.eta = source_artifact.downloadProgress.eta

  def _calculate_eta(self, filename: str, progress: float) -> int:
    """Calculate ETA based on elapsed time and current progress"""
    if filename not in self._download_start_times or progress <= 0:
      return 60  # Default ETA for new downloads

    elapsed_time = time.monotonic() - self._download_start_times[filename]
    if elapsed_time <= 0:
      return 60

    # If we're at X% after Y seconds, we can estimate total time as (Y / X) * 100
    total_estimated_time = (elapsed_time / progress) * 100
    eta = total_estimated_time - elapsed_time

    return max(1, int(eta))  # Return at least 1 second if download is ongoing

  async def _download_file(self, url: str, path: str, model) -> None:
    """Downloads a file with progress tracking"""
    self._download_start_times[model.fileName] = time.monotonic()

    async with aiohttp.ClientSession() as session:
      async with session.get(url) as response:
        response.raise_for_status()
        total_size = int(response.headers.get("content-length", 0))
        bytes_downloaded = 0

        with open(path, 'wb') as f:
          async for chunk in response.content.iter_chunked(self._chunk_size):  # type: bytes
            f.write(chunk)
            bytes_downloaded += len(chunk)

            # BluePilot: support cancellation of background favorite prefetch.
            if self._download_cancelled():
              # End BluePilot
              raise Exception("Download cancelled")

            if total_size > 0:
              progress = (bytes_downloaded / total_size) * 100
              model.downloadProgress.status = custom.ModelManagerSP.DownloadStatus.downloading
              model.downloadProgress.progress = progress
              model.downloadProgress.eta = self._calculate_eta(model.fileName, progress)
              self._sync_artifact_progress(model)
              self._report_status()

        # Clean up start time after download completes
        del self._download_start_times[model.fileName]

  async def _download_chunked(self, base_url: str, base_path: str, artifact) -> None:
    from openpilot.common.file_chunker import get_manifest_path, get_chunk_name
    manifest_url = get_manifest_path(base_url)
    manifest_path = get_manifest_path(base_path)

    async with aiohttp.ClientSession() as session:
      async with session.get(manifest_url) as resp:
        if resp.status == 404:
          raise FileNotFoundError
        resp.raise_for_status()
        num_chunks = int((await resp.read()).strip())

    self._download_start_times[artifact.fileName] = time.monotonic()

    for i in range(num_chunks):
      chunk_url = get_chunk_name(base_url, i, num_chunks)
      chunk_path = get_chunk_name(base_path, i, num_chunks)
      chunk_downloaded = 0
      async with aiohttp.ClientSession() as session:
        async with session.get(chunk_url) as response:
          response.raise_for_status()
          chunk_size = int(response.headers.get("content-length", 0))
          with open(chunk_path, 'wb') as f:
            async for data in response.content.iter_chunked(self._chunk_size):
              f.write(data)
              chunk_downloaded += len(data)
              # BluePilot: support cancellation of background favorite prefetch.
              if self._download_cancelled():
                # End BluePilot
                raise Exception("Download cancelled")
              intra = chunk_downloaded / max(chunk_size, 1)
              progress = min(99, (i + intra) / num_chunks * 100)
              artifact.downloadProgress.status = custom.ModelManagerSP.DownloadStatus.downloading
              artifact.downloadProgress.progress = progress
              artifact.downloadProgress.eta = self._calculate_eta(artifact.fileName, progress)
              self._sync_artifact_progress(artifact)
              self._report_status()

    with open(manifest_path, 'w') as f:
      f.write(str(num_chunks))
    if os.path.isfile(base_path):
      os.remove(base_path)
    del self._download_start_times[artifact.fileName]

  async def _process_artifact(self, artifact, destination_path: str) -> None:
    url = artifact.downloadUri.uri
    filename = artifact.fileName

    try:
      # BluePilot: verify locally first; failed downloads leave cached files intact.
      async def download(staged_path):
        if not url:
          raise ValueError("Uncached model artifact has no download URL")
        try:
          await self._download_chunked(url, staged_path, artifact)
        except (FileNotFoundError, aiohttp.ClientResponseError):
          await self._download_file(url, staged_path, artifact)

      downloaded = await ensure_artifact(artifact, destination_path, verify_file, download, self._download_cancelled)
      artifact.downloadProgress.status = (custom.ModelManagerSP.DownloadStatus.downloaded if downloaded else
                                         custom.ModelManagerSP.DownloadStatus.cached)
      # End BluePilot
      artifact.downloadProgress.progress = 100
      artifact.downloadProgress.eta = 0
      self._sync_artifact_progress(artifact)
      self._report_status()

    except Exception as e:
      cloudlog.error(f"Error downloading {filename}: {str(e)}")
      # BluePilot: failed temporary downloads are cleaned by TemporaryDirectory;
      # keep previously verified cache files (including other bundles' chunks).
      # End BluePilot
      artifact.downloadProgress.status = custom.ModelManagerSP.DownloadStatus.failed
      artifact.downloadProgress.eta = 0
      self._sync_artifact_progress(artifact)
      self.selected_bundle.status = custom.ModelManagerSP.DownloadStatus.failed
      self._report_status()
      self._download_start_times.pop(artifact.fileName, None)
      raise

  async def _process_model(self, model, destination_path: str) -> None:
    """Processes a single model download including verification"""
    model_artifact = model.artifact
    metadata_artifact = model.metadata

    await self._process_artifact(metadata_artifact, destination_path)
    await self._process_artifact(model_artifact, destination_path)

  def _report_status(self) -> None:
    """Reports current status through messaging system"""
    # BluePilot: the background cache worker has no modelManagerSP publisher.
    if self.cache_only:
      return
    # End BluePilot
    msg = messaging.new_message('modelManagerSP', valid=True)
    model_manager_state = msg.modelManagerSP
    if self.selected_bundle:
      model_manager_state.selectedBundle = self.selected_bundle

    # BluePilot: during warmup report the previous selection, not the candidate.
    if bundle := displayed_bundle(self.params):
      model_manager_state.activeBundle = bundle
    # End BluePilot

    model_manager_state.availableBundles = self.available_models
    self.pm.send('modelManagerSP', msg)

  async def _download_bundle(self, model_bundle: custom.ModelManagerSP.ModelBundle, destination_path: str) -> None:
    """Downloads all models in a bundle"""
    self.selected_bundle = model_bundle
    self.selected_bundle.status = custom.ModelManagerSP.DownloadStatus.downloading
    os.makedirs(destination_path, exist_ok=True)

    try:
      # BluePilot: downloads must not overwrite artifacts needed for rollback.
      protected = {}
      if active := get_active_bundle(self.params):
        for model in active.models:
          for artifact in (model.metadata, model.artifact):
            if artifact.fileName:
              protected[artifact.fileName] = artifact.downloadUri.sha256
      for bundle in (self.params.get(FAVORITES) or {}).values():
        for model in bundle.get('models', []):
          for artifact in (model.get('metadata', {}), model.get('artifact', {})):
            if artifact.get('fileName'):
              filename = artifact['fileName']
              sha = artifact.get('downloadUri', {}).get('sha256', '')
              if filename in protected and protected[filename] != sha:
                raise ValueError("Favorite models have conflicting artifact filenames")
              protected[filename] = sha
      for model in self.selected_bundle.models:
        for artifact in (model.metadata, model.artifact):
          if artifact.fileName in protected and artifact.downloadUri.sha256 != protected[artifact.fileName]:
            raise ValueError("Model download would overwrite an active model artifact")
          if artifact.fileName:
            protected[artifact.fileName] = artifact.downloadUri.sha256
      # End BluePilot
      seen_artifacts: set[str] = set()
      for model in self.selected_bundle.models:
        for artifact in (model.metadata, model.artifact):
          # BluePilot: stop prefetch before hashing another large artifact onroad.
          if self.cache_only and self._download_cancelled():
            raise RuntimeError("Favorite prefetch paused")
          # End BluePilot
          if not artifact.fileName:
            continue
          if artifact.fileName in seen_artifacts:
            artifact.downloadProgress.status = custom.ModelManagerSP.DownloadStatus.cached
            artifact.downloadProgress.progress = 100
            artifact.downloadProgress.eta = 0
          else:
            seen_artifacts.add(artifact.fileName)
            await self._process_artifact(artifact, destination_path)

      # BluePilot: only manager may activate a verified candidate.
      self.selected_bundle.status = custom.ModelManagerSP.DownloadStatus.downloaded
      if not self.cache_only:
        stage_bundle(self.params, self.selected_bundle.to_dict())
      # End BluePilot
      self.selected_bundle = None

    except Exception:
      self.selected_bundle.status = custom.ModelManagerSP.DownloadStatus.failed
      raise

    finally:
      self._report_status()

  def download(self, model_bundle: custom.ModelManagerSP.ModelBundle, destination_path: str) -> None:
    """Main entry point for downloading a model bundle"""
    asyncio.run(self._download_bundle(model_bundle, destination_path))

  def main_thread(self) -> None:
    """Main thread for model management"""
    rk = Ratekeeper(1, print_delay_threshold=None)

    while True:
      try:
        # BluePilot: all catalog networking runs in the independent cache worker.
        self.available_models = include_pinned_favorites(self.params, self.model_fetcher.get_available_bundles(allow_network=False))
        # End BluePilot
        # BluePilot: never mutate the running selection or rollback target onroad.
        if not self.params.get_bool("IsOnroad") and self.params.get(TRANSACTION) is None:
          validate_active_bundle(self.params, self.available_models)
        # End BluePilot
        self.active_bundle = get_active_bundle(self.params)

        if (index_to_download := self.params.get("ModelManager_DownloadIndex")) is not None:
          # BluePilot: serialize requests through activation and rollback.
          if self.params.get(PENDING) is not None or self.params.get(TRANSACTION) is not None:
            self.params.remove("ModelManager_DownloadIndex")
            rk.keep_time()
            continue
          # End BluePilot
          if model_to_download := next((model for model in self.available_models if model.index == index_to_download), None):
            try:
              self.download(model_to_download, Paths.model_root())
            except Exception as e:
              cloudlog.exception(e)
              # BluePilot: leave a durable, visible outcome without changing the model.
              cancelled = self.params.get('ModelManager_DownloadIndex') is None
              self.params.put(STATUS, 'cancelled' if cancelled else 'download_failed', block=True)
              if cancelled:
                self.selected_bundle = None
              # End BluePilot
            finally:
              self.params.remove("ModelManager_DownloadIndex")
          # BluePilot: a stale catalog index must not permanently disable selection.
          else:
            self.params.remove("ModelManager_DownloadIndex")
            self.params.put(STATUS, 'download_failed', block=True)
          # End BluePilot

        if self.params.get("ModelManager_ClearCache"):
          self.clear_model_cache()
          self.params.remove("ModelManager_ClearCache")

        self._report_status()
        rk.keep_time()

      except Exception as e:
        cloudlog.exception(f"Error in main thread: {str(e)}")
        rk.keep_time()

  def clear_model_cache(self) -> None:
    """
    Clears the model cache directory of all files except those in the active model bundle.
    """

    # BluePilot: keep running, staged, and rollback artifacts intact.
    if self.params.get_bool("IsOnroad") or self.params.get(PENDING) is not None or self.params.get(TRANSACTION) is not None:
      return
    # End BluePilot

    # Get list of files used by active model bundle
    active_files = []
    # BluePilot: retain complete favorite artifacts, including pinned catalog entries.
    for bundle in (self.params.get(FAVORITES) or {}).values():
      for model in bundle.get('models', []):
        for artifact in (model.get('artifact', {}), model.get('metadata', {})):
          if artifact.get('fileName'):
            active_files.append(artifact['fileName'])
    # End BluePilot
    if self.active_bundle is not None: # When the default model is active
      for model in self.active_bundle.models:
        if hasattr(model, 'artifact') and model.artifact.fileName:
          active_files.append(model.artifact.fileName)
        if hasattr(model, 'metadata') and model.metadata.fileName:
          active_files.append(model.metadata.fileName)

    # Remove all files except active ones (including their chunk files)
    model_dir = Paths.model_root()
    try:
      for filename in os.listdir(model_dir):
        base = filename.split('.chunk')[0] if '.chunk' in filename else filename
        if base not in active_files and filename not in active_files:
          file_path = os.path.join(model_dir, filename)
          if os.path.isfile(file_path):
            os.remove(file_path)
      cloudlog.info("Model cache cleared, keeping active model files")
    except Exception as e:
      cloudlog.exception(f"Error clearing model cache: {str(e)}")

def main():
  ModelManagerSP().main_thread()


if __name__ == "__main__":
  main()
