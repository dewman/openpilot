"""File operations shared by foreground downloads and favorite prefetch."""
import os
import tempfile
import hashlib
from pathlib import Path

from openpilot.common.file_chunker import get_manifest_path, get_chunk_name


async def ensure_artifact(artifact, destination, verify, download, cancelled):
  """Return whether downloaded; a valid cache hit never invokes the network callback."""
  filename = artifact.fileName
  if not filename or filename in ('.', '..') or os.path.basename(filename) != filename:
    raise ValueError("Invalid model artifact filename")
  target = os.path.join(destination, filename)
  expected_hash = artifact.downloadUri.sha256
  if await verify(target, expected_hash):
    return False
  with tempfile.TemporaryDirectory(prefix='.bp-download-', dir=destination) as staging:
    staged = os.path.join(staging, filename)
    await download(staged)
    if not await verify(staged, expected_hash):
      raise ValueError(f"Hash validation failed for {filename}")
    if cancelled():
      raise RuntimeError("Download cancelled")
    publish_artifact(staging, destination, filename)
  return True


def file_sha256(path):
  """Stream large model files/chunks without holding a second model in RAM."""
  try:
    paths = [path]
    if os.path.isfile(manifest := get_manifest_path(path)):
      count = int(Path(manifest).read_text())
      if count <= 0:
        return None
      paths = [get_chunk_name(path, i, count) for i in range(count)]
    digest = hashlib.sha256()
    for file in paths:
      with open(file, 'rb') as stream:
        while block := stream.read(1024 * 1024):
          digest.update(block)
    return digest.hexdigest()
  except (OSError, ValueError):
    return None


def publish_artifact(staging, destination, filename):
  """Publish verified pieces before their manifest; leave failed downloads isolated."""
  staged = os.path.join(staging, filename)
  target = os.path.join(destination, filename)
  manifest = get_manifest_path(staged)
  if os.path.isfile(manifest):
    count = int(Path(manifest).read_text())
    for i in range(count):
      os.replace(get_chunk_name(staged, i, count), get_chunk_name(target, i, count))
    os.replace(manifest, get_manifest_path(target))
    if os.path.isfile(target):
      os.unlink(target)
  else:
    os.replace(staged, target)
    # read_file_chunked prefers a manifest, so remove one from an older representation.
    if os.path.isfile(old_manifest := get_manifest_path(target)):
      os.unlink(old_manifest)


def bundle_signature(bundle, directory):
  """Cheap change detection; a matching signature never replaces initial SHA verification."""
  signature = []
  try:
    for model in bundle.get('models', []):
      for artifact in (model.get('metadata', {}), model.get('artifact', {})):
        if not artifact.get('downloadUri', {}).get('uri'):
          continue
        path = os.path.join(directory, artifact['fileName'])
        paths = [path]
        if os.path.isfile(manifest := get_manifest_path(path)):
          count = int(Path(manifest).read_text())
          paths = [manifest, *(get_chunk_name(path, i, count) for i in range(count))]
        for file in paths:
          stat = os.stat(file)
          signature.append((file, stat.st_size, stat.st_mtime_ns))
    return tuple(signature) if signature else None
  except (OSError, ValueError):
    return None
