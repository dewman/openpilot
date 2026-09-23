"""Requires built Params/msgq extensions; tests the real engagement machines and downloader."""
import hashlib
from types import SimpleNamespace

import pytest
from cereal import car, custom, log
from openpilot.common.params import Params
from openpilot.selfdrive.selfdrived.events import Events
from openpilot.selfdrive.selfdrived.state import StateMachine
from openpilot.sunnypilot.mads.state import StateMachine as MadsStateMachine
from openpilot.sunnypilot.selfdrive.selfdrived.events import EventsSP
from openpilot.sunnypilot.models import manager as manager_module
from openpilot.sunnypilot.models.helpers import REQUIRED_JSON_VERSION
from openpilot.bluepilot.models.switch import ACTIVE, PENDING, TRANSACTION
from openpilot.bluepilot.models.engagement import ModelSwitchEngagementBP
from openpilot.bluepilot.models.favorites import FAVORITES


@pytest.mark.parametrize('state', log.SelfdriveState.OpenpilotState.schema.enumerants.values())
def test_real_stock_state_machine_cannot_engage_during_switch(tmp_path, state):
  params = Params(str(tmp_path / 'params'))
  params.put(TRANSACTION, {'id': 'locked'}, block=True)
  events = Events()
  events.add(log.OnroadEvent.EventName.buttonEnable)
  ModelSwitchEngagementBP(params).update(car.CarState.new_message(vEgo=30.), events)
  machine = StateMachine()
  machine.state = state
  enabled, active = machine.update(events)
  assert not enabled and not active


@pytest.mark.parametrize('state', custom.ModularAssistiveDrivingSystem.ModularAssistiveDrivingSystemState.schema.enumerants.values())
def test_real_mads_state_machine_cannot_engage_during_switch(tmp_path, state):
  params = Params(str(tmp_path / 'params'))
  params.put(TRANSACTION, {'id': 'locked'}, block=True)
  events = Events()
  events_sp = EventsSP()
  events_sp.add(custom.OnroadEventSP.EventName.lkasEnable)
  ModelSwitchEngagementBP(params).update(car.CarState.new_message(vEgo=30.), events)
  selfdrive = SimpleNamespace(events=events, events_sp=events_sp, enabled=False, state_machine=StateMachine())
  machine = MadsStateMachine(SimpleNamespace(selfdrive=selfdrive))
  machine.state = state
  enabled, active = machine.update()
  assert not enabled and not active


@pytest.fixture
def cached_manager(tmp_path, monkeypatch):
  params = Params(str(tmp_path / 'params'))
  weights = tmp_path / 'weights'
  weights.mkdir()
  monkeypatch.setattr(manager_module, 'Params', lambda: params)
  monkeypatch.setattr(manager_module.Paths, 'model_root', lambda: str(weights))
  content = b'verified cached driving model'
  (weights / 'driving.pkl').write_bytes(content)
  bundle = custom.ModelManagerSP.ModelBundle.new_message(
    ref='favorite', index=123, runner='tinygrad', minimumSelectorVersion=REQUIRED_JSON_VERSION,
    models=[{'type': 'supercombo', 'artifact': {'fileName': 'driving.pkl', 'downloadUri': {
      'uri': 'https://invalid.example/driving.pkl', 'sha256': hashlib.sha256(content).hexdigest()}}}])
  manager = manager_module.ModelManagerSP(cache_only=True)
  params.put('ModelManager_Favs', 'favorite', block=True)
  params.put(FAVORITES, {'favorite': bundle.to_dict()}, block=True)

  def no_network(*args, **kwargs):
    raise AssertionError('Cached selection accessed the network')

  monkeypatch.setattr(manager_module.aiohttp, 'ClientSession', no_network)
  monkeypatch.setattr(manager.model_fetcher, '_fetch_and_cache_models', no_network)
  return manager, params, bundle, weights


def test_cached_download_stages_without_network_or_activating(cached_manager):
  manager, params, bundle, weights = cached_manager
  manager.cache_only = False
  manager.pm = SimpleNamespace(send=lambda *args: None)
  params.put('ModelManager_DownloadIndex', bundle.index, block=True)
  manager.download(bundle, str(weights))
  assert params.get(ACTIVE) is None
  assert params.get(PENDING)['bundle']['ref'] == bundle.ref


def test_background_prefetch_never_selects_model(cached_manager):
  manager, params, bundle, weights = cached_manager
  manager.download(bundle, str(weights))
  assert params.get(ACTIVE) is None
  assert params.get(PENDING) is None


def test_local_catalog_read_does_not_attempt_refresh(cached_manager):
  manager, params, _, _ = cached_manager
  params.put('ModelManager_LastSyncTime', 0, block=True)
  assert manager.model_fetcher.get_available_bundles(allow_network=False) == []


def test_clear_cache_preserves_favorites_and_their_chunks(cached_manager):
  manager, _, _, weights = cached_manager
  (weights / 'driving.pkl.chunk01of01').write_bytes(b'favorite chunk')
  (weights / 'unused.pkl').write_bytes(b'unused')
  manager.clear_model_cache()
  assert (weights / 'driving.pkl').exists()
  assert (weights / 'driving.pkl.chunk01of01').exists()
  assert not (weights / 'unused.pkl').exists()


def test_prefetch_yields_to_onroad_or_foreground_requests(cached_manager):
  manager, params, bundle, weights = cached_manager
  params.put_bool('IsOnroad', True, block=True)
  with pytest.raises(RuntimeError, match='prefetch paused'):
    manager.download(bundle, str(weights))
  params.put_bool('IsOnroad', False, block=True)
  params.put('ModelManager_DownloadIndex', 999, block=True)
  with pytest.raises(RuntimeError, match='prefetch paused'):
    manager.download(bundle, str(weights))
  assert params.get(PENDING) is None


def test_conflicting_download_cannot_overwrite_cached_favorite(cached_manager):
  manager, params, bundle, weights = cached_manager
  original = (weights / 'driving.pkl').read_bytes()
  bundle.models[0].artifact.downloadUri.sha256 = hashlib.sha256(b'different').hexdigest()
  with pytest.raises(ValueError, match='overwrite'):
    manager.download(bundle, str(weights))
  assert (weights / 'driving.pkl').read_bytes() == original
  assert params.get(PENDING) is None


def test_cache_clear_during_switch_cannot_delete_rollback_files(cached_manager):
  manager, params, _, weights = cached_manager
  (weights / 'previous.pkl').write_bytes(b'previous')
  params.put(TRANSACTION, {'id': 'locked'}, block=True)
  manager.clear_model_cache()
  assert (weights / 'previous.pkl').exists()


@pytest.fixture
def selector(tmp_path, monkeypatch):
  from openpilot.selfdrive.ui.sunnypilot.layouts.settings import models

  params = Params(str(tmp_path / 'selector_params'))
  state = SimpleNamespace(params=params, engaged=False, sm={'carState': car.CarState.new_message(vEgo=30.)}, is_offroad=lambda: False)
  monkeypatch.setattr(models, 'ui_state', state)
  monkeypatch.setattr(models, 'alert_dialog', lambda message: message)
  monkeypatch.setattr(models.gui_app, 'push_widget', lambda *args: None)
  layout = models.ModelsLayout.__new__(models.ModelsLayout)
  layout.model_dialog = SimpleNamespace(selection_ref='Default')
  return models, layout, state, params


def test_picker_rechecks_engagement_at_confirmation(selector):
  models, layout, state, params = selector
  state.engaged = True
  layout._on_model_selected(models.DialogResult.CONFIRM)
  assert params.get(PENDING) is None
  assert params.get('ModelManager_DownloadIndex') is None


def test_onroad_default_selection_stages_without_resetting_calibration(selector, monkeypatch):
  models, layout, _, params = selector

  def unexpected_reset():
    raise AssertionError('Onroad selection must preserve calibration')

  monkeypatch.setattr(layout, '_show_reset_params_dialog', unexpected_reset)
  layout._on_model_selected(models.DialogResult.CONFIRM)
  assert params.get(PENDING) == {'bundle': {}}
  assert layout.model_dialog is None
