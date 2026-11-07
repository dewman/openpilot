"""Offline verification, atomic downloads, pinning and real engagement regressions."""
import asyncio

# Tiny synchronous fixture writes exercise the production async callback API.
# ruff: noqa: ASYNC240
import hashlib
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from openpilot.cereal import custom, log
from opendbc.car.structs import car
from openpilot.bluepilot.models.cache import FAVORITES, CACHE_STATUS, ensure_artifact, ready_bundle, signature
from openpilot.bluepilot.models.manager import ModelManagerBP
from openpilot.bluepilot.models.switch import TRANSACTION, REENGAGE, stage_bundle
from openpilot.bluepilot.models.engagement import ModelSwitchEngagementBP
from openpilot.common.params import Params
from openpilot.sunnypilot.models.helpers import REQUIRED_JSON_VERSION
from openpilot.selfdrive.selfdrived.events import Events
from openpilot.selfdrive.selfdrived.state import StateMachine
from openpilot.sunnypilot.mads.mads import ModularAssistiveDrivingSystem
from openpilot.system.manager.process import PythonProcess
from openpilot.bluepilot.models.warmup import run_worker

BODY = b'cached model weights'
SHA = hashlib.sha256(BODY).hexdigest()


@pytest.fixture
def params(tmp_path):
  result = Params(str(tmp_path / 'params'))
  result.put_bool('IsOffroad', True, block=True)
  return result


def raw_bundle(ref='test'):
  return custom.ModelManagerSP.ModelBundle(
    ref=ref, displayName=ref, runner='tinygrad', minimumSelectorVersion=REQUIRED_JSON_VERSION,
    models=[{'artifact': {'fileName': 'model.pkl', 'downloadUri': {'uri': 'https://invalid.example/model', 'sha256': SHA}}}],
  ).to_dict()


def manager(params):
  result = ModelManagerBP.__new__(ModelManagerBP)
  result.params = params
  result.prefetching = False
  result.verified, result.retry_at = {}, {}
  result._download_start_times = {}
  result.selected_bundle = None
  result._report_status = Mock()
  result.source_models = {'qcom': [custom.ModelManagerSP.ModelBundle(**raw_bundle())]}
  return result


@pytest.mark.parametrize('failure', ['network', 'hash', 'cancelled'])
def test_failed_transfer_preserves_existing_artifact(tmp_path, failure):
  path = tmp_path / 'model.pkl'
  path.write_bytes(b'previous model')
  artifact = custom.ModelManagerSP.Artifact(**raw_bundle()['models'][0]['artifact'])
  async def verify(target, sha):
    return Path(target).exists() and hashlib.sha256(Path(target).read_bytes()).hexdigest() == sha
  async def download(target):
    Path(target).write_bytes(BODY if failure == 'cancelled' else b'partial')
    if failure == 'network':
      raise OSError('network unavailable')
  with pytest.raises((OSError, RuntimeError, ValueError)):
    asyncio.run(ensure_artifact(artifact, tmp_path, verify, download, lambda: failure == 'cancelled'))
  assert path.read_bytes() == b'previous model'
  assert list(tmp_path.iterdir()) == [path]


def test_cache_hit_never_calls_network(tmp_path):
  path = tmp_path / 'model.pkl'
  path.write_bytes(BODY)
  artifact = custom.ModelManagerSP.Artifact(**raw_bundle()['models'][0]['artifact'])
  async def verify(target, sha):
    return hashlib.sha256(Path(target).read_bytes()).hexdigest() == sha
  network = Mock(side_effect=AssertionError('Network used for cached selection'))
  assert not asyncio.run(ensure_artifact(artifact, tmp_path, verify, network, lambda: False))
  network.assert_not_called()


def test_prefetch_caches_all_favorites_without_changing_selection(params, tmp_path):
  model_dir = tmp_path / 'models'
  instance = manager(params)
  second = raw_bundle('second')
  second['models'][0]['artifact']['fileName'] = 'second.pkl'
  instance.source_models['qcom'].append(custom.ModelManagerSP.ModelBundle(**second))
  params.put('ModelManager_Favs', 'test;second', block=True)
  params.put('ModelManager_ActiveBundle', {'ref': 'current'}, block=True)
  async def download(url, target, artifact):
    Path(target).write_bytes(BODY)
  instance._download_file = download
  with patch('openpilot.bluepilot.models.manager.Paths.model_root', return_value=str(model_dir)):
    instance._idle_tasks()
    for bundle in (raw_bundle(), second):
      assert ready_bundle(params, bundle, 'qcom')
    assert params.get('ModelManager_ActiveBundle') == {'ref': 'current'}
    assert params.get('ModelManager_ActiveBundleChestnut') is None
    # A removed catalog entry remains selectable offline from the pinned metadata.
    instance.source_models['qcom'] = []
    instance._idle_tasks()
    assert set(params.get(FAVORITES)) == {'test', 'second'}
    (model_dir / 'model.pkl').write_bytes(b'changed')
    assert not ready_bundle(params, raw_bundle(), 'qcom')
    # Clear Cache retains every favorite and active model, including derived caches.
    (model_dir / 'second.pkl.cache').write_bytes(b'cache')
    (model_dir / 'unused').write_bytes(b'unused')
    instance.clear_model_cache()
    assert (model_dir / 'second.pkl.cache').exists()
    assert not (model_dir / 'unused').exists()


def test_prefetch_does_not_run_onroad(params, tmp_path):
  instance = manager(params)
  params.put('ModelManager_Favs', 'test', block=True)
  params.put_bool('IsOffroad', False, block=True)
  instance._download_file = Mock(side_effect=AssertionError('Onroad prefetch'))
  with patch('openpilot.bluepilot.models.manager.Paths.model_root', return_value=str(tmp_path)):
    instance._idle_tasks()
  instance._download_file.assert_not_called()
  assert params.get(CACHE_STATUS)['test']['state'] == 'pending'


def test_stage_requires_favorite_source_and_unchanged_verified_files(params, tmp_path):
  bundle = raw_bundle()
  (tmp_path / 'model.pkl').write_bytes(BODY)
  params.put('ModelManager_Favs', 'test', block=True)
  params.put(FAVORITES, {'test': {'source': 'qcom', 'bundle': bundle}}, block=True)
  params.put(CACHE_STATUS, {'test': {'state': 'ready', 'signature': signature(bundle, tmp_path)}}, block=True)
  with patch('openpilot.common.hardware.hw.Paths.model_root', return_value=str(tmp_path)):
    assert ready_bundle(params, bundle, 'qcom')
    assert not ready_bundle(params, bundle, 'chestnut')
    params.put('ModelManager_Favs', '', block=True)
    with pytest.raises(ValueError):
      stage_bundle(params, bundle, 'qcom')
    params.put('ModelManager_Favs', 'test', block=True)
    (tmp_path / 'model.pkl').unlink()
    with pytest.raises(ValueError):
      stage_bundle(params, bundle, 'qcom')
    assert params.get(TRANSACTION) is None


@pytest.mark.parametrize('state_name', ['disabled', 'enabled', 'softDisabling', 'overriding', 'preEnabled'])
def test_stock_engagement_lock_uses_real_state_machine(params, state_name):
  switch = ModelSwitchEngagementBP(params)
  state_machine = StateMachine()
  state_machine.state = getattr(log.SelfdriveState.OpenpilotState, state_name)
  params.put(TRANSACTION, {'id': 'lock'}, block=True)
  events = Events()
  events.add(log.OnroadEvent.EventName.buttonEnable)
  switch.update(car.CarState(), events)
  enabled, active = state_machine.update(events)
  assert not enabled and not active


@pytest.mark.parametrize('state_name', ['disabled', 'paused', 'enabled', 'softDisabling', 'overriding'])
def test_mads_interlock_overrides_automatic_enable_and_pause(params, state_name):
  switch = ModelSwitchEngagementBP(params)
  params.put(TRANSACTION, {'id': 'lock'}, block=True)
  events = Events()
  events.add(log.OnroadEvent.EventName.wrongGear)
  switch.update(car.CarState(), events)
  instance = ModularAssistiveDrivingSystem.__new__(ModularAssistiveDrivingSystem)
  instance.enabled_toggle = True
  instance.data_sample = Mock()
  instance.update_events = Mock()
  instance.state_machine = SimpleNamespace(state=getattr(custom.ModularAssistiveDrivingSystem.ModularAssistiveDrivingSystemState, state_name))
  instance.selfdrive = SimpleNamespace(bp_model_switch=switch, enabled=False)
  instance.enabled = instance.active = True
  instance.update(car.CarState())
  assert not instance.enabled and not instance.active
  assert instance.state_machine.state == custom.ModularAssistiveDrivingSystem.ModularAssistiveDrivingSystemState.disabled


def test_params_lock_survives_restart_and_transition_clears(params):
  from openpilot.common.params import ParamKeyFlag
  params.put(TRANSACTION, {'id': 'durable'}, block=True)
  params.put_bool(REENGAGE, True, block=True)
  for flags in (ParamKeyFlag.CLEAR_ON_MANAGER_START, ParamKeyFlag.CLEAR_ON_ONROAD_TRANSITION,
                ParamKeyFlag.CLEAR_ON_OFFROAD_TRANSITION):
    params.clear_all(flags)
  restored = Params(str(Path(params.get_param_path()).parent))
  assert restored.get(TRANSACTION) == {'id': 'durable'}
  assert restored.get_bool(REENGAGE)


def test_repeated_nonblocking_stop_never_joins_and_escalates():
  process = PythonProcess('test', 'unused', lambda *args: False)
  process.proc = SimpleNamespace(exitcode=None, pid=123)
  process.signal = Mock()
  process.stop(block=False)
  with patch('openpilot.system.manager.process.join_process', side_effect=AssertionError('blocking join')):
    process.stop(block=False)
    process._stop_requested_at = time.monotonic() - 6
    process.stop(block=False)
  assert process.signal.call_count == 2  # SIGINT, then SIGKILL


def test_warmup_worker_is_cleaned_up_when_ignition_turns_on(params):
  params.put_bool('IsOffroad', False, block=True)
  worker = Mock()
  worker.is_alive.side_effect = [True, True, False]
  run_worker(worker, params)
  worker.terminate.assert_called_once()
  worker.join.assert_called_once_with(timeout=1)


def test_bp_exp_pins_and_status_are_migrated_without_trusting_old_ready_flag(params, tmp_path):
  from openpilot.bluepilot.models.cache import pinned_favorites, offline_status
  params.put('ModelManager_Favs', 'test', block=True)
  params.put(FAVORITES, {'test': raw_bundle()}, block=True)
  params.put(CACHE_STATUS, {'test': 'ready'}, block=True)
  assert pinned_favorites(params)['test']['source'] == 'qcom'
  assert '0/1' in offline_status(params)
  with patch('openpilot.common.hardware.hw.Paths.model_root', return_value=str(tmp_path)):
    assert not ready_bundle(params, raw_bundle(), 'qcom')
    instance = manager(params)
    instance.source_models = {'qcom': []}
    (tmp_path / 'model.pkl').write_bytes(BODY)
    instance._idle_tasks()
    assert ready_bundle(params, raw_bundle(), 'qcom')
  assert params.get(FAVORITES)['test']['bundle'] == raw_bundle()


def test_old_incompatible_pin_is_dropped(params):
  from openpilot.bluepilot.models.cache import pinned_favorites
  bundle = raw_bundle()
  bundle['minimumSelectorVersion'] = REQUIRED_JSON_VERSION - 1
  params.put(FAVORITES, {'test': bundle}, block=True)
  assert pinned_favorites(params) == {}


def test_downloaded_favorite_requires_successful_runtime_preparation_before_staging(params, tmp_path):
  from openpilot.bluepilot.models.switch import PENDING
  from openpilot.bluepilot.models.warmup import publish_ready, switch_ready_bundle
  bundle = raw_bundle()
  (tmp_path / 'model.pkl').write_bytes(BODY)
  (tmp_path / 'model.pkl.camera.cache').write_bytes(b'compiled gpu graph')
  params.put('ModelManager_Favs', 'test', block=True)
  params.put(FAVORITES, {'test': {'source': 'qcom', 'bundle': bundle}}, block=True)
  params.put(CACHE_STATUS, {'test': {'state': 'ready', 'signature': signature(bundle, tmp_path)}}, block=True)
  with (patch('openpilot.common.hardware.hw.Paths.model_root', return_value=str(tmp_path)),
        patch('openpilot.bluepilot.models.warmup.preparation_identity', return_value='engine'),
        patch('openpilot.bluepilot.models.warmup.prepared_cache_paths', return_value=['model.pkl.camera.cache'])):
    assert ready_bundle(params, bundle, 'qcom')
    assert not switch_ready_bundle(params, bundle, 'qcom')
    with pytest.raises(ValueError):
      stage_bundle(params, bundle, 'qcom')
    publish_ready(bundle, 'qcom', tmp_path, signature(bundle, tmp_path))
    assert switch_ready_bundle(params, bundle, 'qcom')
    stage_bundle(params, bundle, 'qcom')
    assert params.get(PENDING)['bundle'] == bundle
    params.remove(PENDING)
    with patch('openpilot.bluepilot.models.warmup.preparation_identity', return_value='new-engine'):
      assert not switch_ready_bundle(params, bundle, 'qcom')
    (tmp_path / 'model.pkl.camera.cache').unlink()
    assert not switch_ready_bundle(params, bundle, 'qcom')


def test_prepare_camera_uses_requested_bundle_and_runs_real_model_warmup():
  from openpilot.bluepilot.models.warmup import prepare_camera
  bundle = raw_bundle()
  # The loader module has GPU imports; substitute its public constructor so this
  # regression cannot start inference or change the user's active model selection.
  import sys
  module = SimpleNamespace(ModelState=Mock())
  with (patch.dict(sys.modules, {'openpilot.sunnypilot.modeld_v2.modeld': module}),
        patch.dict('os.environ', {'COMBINED_MODEL_PKL': 'debug-model'})):
    prepare_camera(bundle, 'qcom', 1344, 760)
  args, kwargs = module.ModelState.call_args
  assert args == (1344, 760) and kwargs['chestnut'] is False
  assert kwargs['model_bundle'].ref == 'test'
  module.ModelState.return_value.warmup.assert_called_once()
