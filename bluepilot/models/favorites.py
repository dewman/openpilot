"""Persistent favorite manifests and background offline-model preparation."""
import time
import os

FAVORITES = "BPFavoriteModelBundles"
CACHE_STATUS = "BPFavoriteModelStatus"


def favorite_refs(params):
  return set(filter(None, (params.get('ModelManager_Favs') or '').split(';')))


def favorite_bundles(params, available):
  """Pin favorite metadata as well as weights so catalog removal does not erase it."""
  pinned = params.get(FAVORITES) or {}
  by_ref = {b.ref: b.to_dict() for b in available}
  return {ref: pinned.get(ref, by_ref.get(ref)) for ref in favorite_refs(params) if ref in pinned or ref in by_ref}


def include_pinned_favorites(params, available):
  from cereal import custom
  pinned = favorite_bundles(params, available)
  result = [custom.ModelManagerSP.ModelBundle(**pinned.pop(b.ref)) if b.ref in pinned else b for b in available]
  result.extend(custom.ModelManagerSP.ModelBundle(**bundle) for bundle in pinned.values())
  return result


def offline_status(params):
  refs = favorite_refs(params)
  statuses = params.get(CACHE_STATUS) or {}
  ready = sum(statuses.get(ref) == 'ready' for ref in refs)
  if any(statuses.get(ref) == 'downloading' for ref in refs):
    return f"{ready}/{len(refs)} Favorites ready offline. Downloading remaining Favorites."
  if any(statuses.get(ref) == 'failed' for ref in refs):
    return f"{ready}/{len(refs)} Favorites ready offline. Download failed; reconnect to retry."
  if ready < len(refs):
    return f"{ready}/{len(refs)} Favorites ready offline. Remaining downloads run offroad when connected."
  return f"{ready}/{len(refs)} Favorites ready offline."


def main():
  # This worker owns network catalog refreshes and favorite prefetch. The foreground
  # model manager reads only the local catalog, so a network timeout cannot hold up
  # selecting a cached model. Large prefetch/verification jobs run only offroad.
  from openpilot.common.params import Params
  from openpilot.common.swaglog import cloudlog
  from openpilot.system.hardware.hw import Paths
  from openpilot.sunnypilot.models.manager import ModelManagerSP
  from openpilot.bluepilot.models.switch import switch_busy
  from openpilot.bluepilot.models.cache import bundle_signature

  os.nice(10)
  params = Params()
  downloader = ModelManagerSP(cache_only=True)
  retry_at = {}
  sync_at = 0.
  statuses = params.get(CACHE_STATUS) or {}
  # Reverify each favorite once per worker start before declaring it available.
  verified = {}
  while True:
    try:
      now = time.monotonic()
      # Save newly marked favorites before a refresh can remove them from the catalog.
      available = downloader.model_fetcher.get_available_bundles(allow_network=False)
      pinned = favorite_bundles(params, available)
      if params.get(FAVORITES) != pinned:
        params.put(FAVORITES, pinned, block=True)
      if now >= sync_at:
        downloader.model_fetcher.get_available_bundles()
        sync_at = now + 30.
      available = downloader.model_fetcher.get_available_bundles(allow_network=False)
      pinned = favorite_bundles(params, available)
      if params.get(FAVORITES) != pinned:
        params.put(FAVORITES, pinned, block=True)
      statuses = {ref: statuses.get(ref, 'pending') for ref in pinned}
      verified = {ref: signature for ref, signature in verified.items() if ref in pinned}
      for ref, bundle in pinned.items():
        signature = bundle_signature(bundle, Paths.model_root())
        if signature is None or (ref in verified and verified[ref] != signature):
          verified.pop(ref, None)
          statuses[ref] = 'pending'

      if not params.get_bool('IsOnroad') and not switch_busy(params):
        for ref, bundle in pinned.items():
          if ref in verified or now < retry_at.get(ref, 0.):
            continue
          if params.get_bool('IsOnroad') or switch_busy(params):
            break
          from cereal import custom
          statuses[ref] = 'downloading'
          params.put(CACHE_STATUS, statuses, block=True)
          try:
            downloader.download(custom.ModelManagerSP.ModelBundle(**bundle), Paths.model_root())
            statuses[ref] = 'ready'
            verified[ref] = bundle_signature(bundle, Paths.model_root())
          except Exception:
            statuses[ref] = 'pending' if params.get_bool('IsOnroad') or switch_busy(params) else 'failed'
            retry_at[ref] = time.monotonic() + 60.
            cloudlog.exception("BluePilot favorite model cache download failed")
          params.put(CACHE_STATUS, statuses, block=True)
      if params.get(CACHE_STATUS) != statuses:
        params.put(CACHE_STATUS, statuses, block=True)
    except Exception:
      cloudlog.exception("BluePilot favorite model cache worker failed")
    time.sleep(1.)


if __name__ == '__main__':
  main()
