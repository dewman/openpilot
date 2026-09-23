import asyncio
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest
from cereal import custom
from openpilot.bluepilot.models.cache import ensure_artifact, file_sha256, publish_artifact, bundle_signature
from openpilot.bluepilot.models.favorites import FAVORITES, CACHE_STATUS, favorite_bundles, include_pinned_favorites, offline_status
from openpilot.common.file_chunker import get_manifest_path, get_chunk_name, read_file_chunked


def write_chunks(path, parts):
  for i, content in enumerate(parts):
    Path(get_chunk_name(str(path), i, len(parts))).write_bytes(content)
  Path(get_manifest_path(str(path))).write_text(str(len(parts)))


async def verify(path, expected):
  return file_sha256(path) == expected


def artifact(content=b'cached model'):
  return SimpleNamespace(fileName='driving.pkl', downloadUri=SimpleNamespace(uri='https://invalid.example/model',
                                                                         sha256=hashlib.sha256(content).hexdigest()))


@pytest.mark.parametrize('chunked', [False, True])
def test_cached_model_never_calls_network(tmp_path, chunked):
  if chunked:
    write_chunks(tmp_path / 'driving.pkl', [b'cached ', b'model'])
  else:
    (tmp_path / 'driving.pkl').write_bytes(b'cached model')

  async def no_network(path):
    raise AssertionError('A fully cached model must not access the network')

  assert not asyncio.run(ensure_artifact(artifact(), str(tmp_path), verify, no_network, lambda: False))


@pytest.mark.parametrize('failure', ['network', 'hash', 'cancelled'])
def test_failed_download_preserves_previous_artifact(tmp_path, failure):
  (tmp_path / 'driving.pkl').write_bytes(b'previous model')

  async def download(path):
    Path(path).write_bytes(b'new model' if failure == 'cancelled' else b'partial')
    if failure == 'network':
      raise ConnectionError('offline')

  with pytest.raises((ValueError, RuntimeError, ConnectionError)):
    asyncio.run(ensure_artifact(artifact(b'new model'), str(tmp_path), verify, download, lambda: failure == 'cancelled'))
  assert (tmp_path / 'driving.pkl').read_bytes() == b'previous model'
  assert [p.name for p in tmp_path.iterdir()] == ['driving.pkl']


@pytest.mark.parametrize('chunked', [False, True])
def test_verified_download_can_switch_cache_representation(tmp_path, chunked):
  if chunked:
    (tmp_path / 'driving.pkl').write_bytes(b'previous model')
  else:
    write_chunks(tmp_path / 'driving.pkl', [b'previous ', b'model'])

  async def download(path):
    if chunked:
      write_chunks(Path(path), [b'new ', b'model'])
    else:
      Path(path).write_bytes(b'new model')

  assert asyncio.run(ensure_artifact(artifact(b'new model'), str(tmp_path), verify, download, lambda: False))
  assert read_file_chunked(str(tmp_path / 'driving.pkl')) == b'new model'
  assert file_sha256(str(tmp_path / 'driving.pkl')) == artifact(b'new model').downloadUri.sha256


def test_chunk_manifest_published_last(tmp_path, monkeypatch):
  staging = tmp_path / 'staging'
  target = tmp_path / 'target'
  staging.mkdir()
  target.mkdir()
  write_chunks(staging / 'driving.pkl', [b'a', b'b'])
  from openpilot.bluepilot.models import cache
  real_replace = cache.os.replace
  calls = []

  def replace(source, destination):
    calls.append(Path(source).name)
    real_replace(source, destination)

  monkeypatch.setattr(cache.os, 'replace', replace)
  publish_artifact(str(staging), str(target), 'driving.pkl')
  assert calls[-1] == 'driving.pkl.chunkmanifest'
  assert read_file_chunked(str(target / 'driving.pkl')) == b'ab'


def test_corrupt_or_missing_chunks_are_not_cache_hits(tmp_path):
  path = tmp_path / 'driving.pkl'
  write_chunks(path, [b'a', b'b'])
  Path(get_chunk_name(str(path), 1, 2)).unlink()
  assert file_sha256(str(path)) is None
  Path(get_manifest_path(str(path))).write_text('invalid manifest')
  assert file_sha256(str(path)) is None


def test_hash_streams_large_artifacts_in_bounded_blocks(tmp_path, monkeypatch):
  content = b'x' * (3 * 1024 * 1024)
  path = tmp_path / 'driving.pkl'
  path.write_bytes(content)
  from openpilot.bluepilot.models import cache
  real_open = open
  reads = []

  class Reader:
    def __enter__(self):
      self.stream = real_open(path, 'rb')
      return self

    def read(self, size):
      reads.append(size)
      return self.stream.read(size)

    def __exit__(self, *args):
      self.stream.close()

  monkeypatch.setattr(cache, 'open', lambda *args: Reader(), raising=False)
  assert file_sha256(str(path)) == hashlib.sha256(content).hexdigest()
  assert all(size == 1024 * 1024 for size in reads)


def test_signature_detects_deleted_or_changed_artifact(tmp_path):
  path = tmp_path / 'driving.pkl'
  path.write_bytes(b'a')
  bundle = {'models': [{'artifact': {'fileName': 'driving.pkl', 'downloadUri': {'uri': 'model'}}}]}
  initial = bundle_signature(bundle, str(tmp_path))
  path.write_bytes(b'longer')
  assert bundle_signature(bundle, str(tmp_path)) != initial
  path.unlink()
  assert bundle_signature(bundle, str(tmp_path)) is None


class Params:
  def __init__(self, values):
    self.values = values

  def get(self, key):
    return self.values.get(key)


def test_pinned_favorite_survives_catalog_removal_and_offline_restart():
  bundle = custom.ModelManagerSP.ModelBundle.new_message(ref='favorite', index=123, runner='tinygrad').to_dict()
  params = Params({'ModelManager_Favs': 'favorite', FAVORITES: {'favorite': bundle}, CACHE_STATUS: {'favorite': 'ready'}})
  assert favorite_bundles(params, []) == {'favorite': bundle}
  available = include_pinned_favorites(params, [])
  assert len(available) == 1 and available[0].ref == 'favorite'
  assert offline_status(params) == '1/1 Favorites ready offline.'


def test_unfavorited_manifest_is_released():
  params = Params({'ModelManager_Favs': '', FAVORITES: {'old': {'ref': 'old'}}})
  assert favorite_bundles(params, []) == {}


def test_incomplete_favorite_is_not_advertised_as_offline_ready():
  params = Params({'ModelManager_Favs': 'a;b', CACHE_STATUS: {'a': 'ready', 'b': 'failed'}})
  assert offline_status(params).startswith('1/2 Favorites ready offline.')
