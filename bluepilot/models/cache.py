"""Verified Favorites on disk. The onroad path performs only cheap stat checks."""
import hashlib
import os
import tempfile
from pathlib import Path

FAVORITES = 'BPFavoriteModelBundles'
CACHE_STATUS = 'BPFavoriteModelStatus'


def favorite_refs(params):
  return set(filter(None, (params.get('ModelManager_Favs') or '').split(';')))


def pinned_favorites(params):
  """Read current and bp-exp pin formats; incompatible manifests are re-fetched."""
  from openpilot.sunnypilot.models.helpers import is_bundle_version_compatible
  result = {}
  for ref, value in (params.get(FAVORITES) or {}).items():
    if not isinstance(value, dict):
      continue
    entry = value if 'bundle' in value else {'source': 'qcom', 'bundle': value}
    bundle = entry.get('bundle')
    if entry.get('source') in ('qcom', 'chestnut') and isinstance(bundle, dict) and is_bundle_version_compatible(bundle):
      result[ref] = entry
  return result


def artifacts(bundle):
  result = {}
  for model in bundle.get('models', []):
    artifact = model.get('artifact', {})
    name, sha = artifact.get('fileName'), artifact.get('downloadUri', {}).get('sha256', '')
    if not name or name in ('.', '..') or os.path.basename(name) != name or len(sha) != 64:
      raise ValueError('Invalid model artifact name or SHA256')
    if name in result and result[name] != sha:
      raise ValueError('Conflicting model artifact hashes')
    result[name] = sha
  if not result:
    raise ValueError('Empty model bundle')
  return result


def signature(bundle, directory):
  try:
    return [[name, sha, stat.st_size, stat.st_mtime_ns, stat.st_ino]
            for name, sha in sorted(artifacts(bundle).items())
            for stat in (os.stat(os.path.join(directory, name)),)]
  except (OSError, ValueError):
    return None


def ready_bundle(params, bundle, source, directory=None):
  if directory is None:
    from openpilot.common.hardware.hw import Paths
    directory = Paths.model_root()
  from openpilot.sunnypilot.models.helpers import is_bundle_version_compatible
  if bundle.get('runner') not in ('stock', 'tinygrad') or not is_bundle_version_compatible(bundle):
    return False
  pinned = pinned_favorites(params).get(bundle.get('ref'))
  status = (params.get(CACHE_STATUS) or {}).get(bundle.get('ref'), {})
  if not isinstance(status, dict):
    return False
  return bool(pinned and pinned['source'] == source and pinned['bundle'] == bundle and
              bundle.get('ref') in favorite_refs(params) and status.get('state') == 'ready' and
              status.get('signature') and status['signature'] == signature(bundle, directory))


async def ensure_artifact(artifact, directory, verify, download, cancelled):
  """Publish atomically: a failed/cancelled transfer cannot truncate a running model."""
  name, sha = artifact.fileName, artifact.downloadUri.sha256
  artifacts({'models': [{'artifact': {'fileName': name, 'downloadUri': {'sha256': sha}}}]})
  target = os.path.join(directory, name)
  if await verify(target, sha):
    return False
  with tempfile.TemporaryDirectory(prefix='.bp-download-', dir=directory) as temporary:
    staged = os.path.join(temporary, name)
    await download(staged)
    if not await verify(staged, sha):
      raise ValueError(f'Hash validation failed for {name}')
    if cancelled():
      raise RuntimeError('Download cancelled')
    os.replace(staged, target)
  return True


def verify_bundle(bundle, directory):
  for name, sha in artifacts(bundle).items():
    with open(Path(directory) / name, 'rb') as stream:
      if hashlib.file_digest(stream, 'sha256').hexdigest().lower() != sha.lower():
        return False
  return True


def offline_status(params):
  refs = favorite_refs(params)
  statuses = params.get(CACHE_STATUS) or {}
  count = sum(isinstance(statuses.get(ref), dict) and statuses[ref].get('state') == 'ready' for ref in refs)
  from openpilot.bluepilot.models.warmup import switch_ready_bundle
  pinned = pinned_favorites(params)
  prepared = sum(switch_ready_bundle(params, entry['bundle'], entry['source']) for entry in pinned.values())
  return f'{count}/{len(refs)} Favorites cached; {prepared} ready to switch. Preparation runs offroad.'
