"""BluePilot Models: cached onroad selection and nonmodal, bounded handover status."""
import time

from openpilot.cereal import custom
from openpilot.bluepilot.models.cache import pinned_favorites, offline_status, ready_bundle
from openpilot.bluepilot.models.switch import STATUS, ERROR, TRANSACTION, RETRY, STATUS_TEXT, stage_bundle, switch_busy
from openpilot.selfdrive.ui.mici.widgets.button import BigButton
from openpilot.selfdrive.ui.mici.widgets.dialog import BigDialog
from openpilot.selfdrive.ui.sunnypilot.mici.layouts.models import ModelsLayoutMici
from openpilot.selfdrive.ui.ui_state import ui_state
from openpilot.system.ui.lib.application import gui_app
from openpilot.system.ui.lib.multilang import tr


def selection_allowed():
  if ui_state.is_offroad():
    return True
  sm = ui_state.sm
  names = ['selfdriveState', 'selfdriveStateSP', 'carState']
  now = time.monotonic()
  return (sm.all_checks(names) and all(0 <= now - sm.logMonoTime[s] / 1e9 < 1 for s in names) and
          not (sm['selfdriveState'].enabled or sm['selfdriveStateSP'].mads.enabled or
               sm['carState'].cruiseState.enabled))


class ModelsLayoutMiciBP(ModelsLayoutMici):
  def __init__(self):
    super().__init__()
    self.retry_btn = BigButton(tr('retry previous model'))
    self.retry_btn.set_click_callback(self._retry_previous)
    self._scroller.add_widget(self.retry_btn)

  def show_event(self):
    if ui_state.params.get(TRANSACTION) is None and not switch_busy(ui_state.params):
      ui_state.params.remove(STATUS)
      ui_state.params.remove(ERROR)
    super().show_event()

  def _retry_previous(self):
    if not selection_allowed():
      return
    ui_state.params.put_bool(RETRY, True, block=True)

  def _show_folders(self):
    if not selection_allowed() or switch_busy(ui_state.params):
      return
    if ui_state.is_offroad():
      super()._show_folders()
      return
    source = 'chestnut' if ui_state.chestnut_present else 'qcom'
    self._selection_source = source
    buttons = []
    pinned = pinned_favorites(ui_state.params)
    for entry in pinned.values():
      bundle = entry['bundle']
      if entry['source'] == source and ready_bundle(ui_state.params, bundle, source):
        btn = BigButton(bundle['displayName'].lower())
        btn.set_click_callback(lambda b=bundle: self._select_model(custom.ModelManagerSP.ModelBundle(**b)))
        buttons.append(btn)
    if buttons:
      self._push_selection_view(buttons)
    else:
      gui_app.push_widget(BigDialog(tr('no cached Favorites'), tr('Mark models as Favorites and let them download offroad first.')))

  def _select_model(self, bundle):
    # Recheck at click time: the car may have engaged while the picker was open.
    if not selection_allowed() or switch_busy(ui_state.params):
      return
    if ui_state.is_offroad():
      super()._select_model(bundle)
      return
    try:
      stage_bundle(ui_state.params, bundle.to_dict(), self._selection_source)
    except ValueError as exc:
      gui_app.push_widget(BigDialog(tr('model unavailable'), str(exc)))
      return
    self._pop_to_main()

  def _select_default(self, source):
    # A picker opened offroad may remain visible across the ignition transition.
    if ui_state.is_offroad() and not switch_busy(ui_state.params):
      super()._select_default(source)

  def _update_state(self):
    super()._update_state()
    params = ui_state.params
    busy = switch_busy(params)
    self.select_model_btn.set_enabled(selection_allowed() and not busy)
    self.select_model_btn.set_value(offline_status(params))
    txn = params.get(TRANSACTION)
    status = params.get(STATUS)
    failed = txn is not None and txn['phase'] == 'failed'
    self.retry_btn.set_visible(failed)
    self.retry_btn.set_enabled(failed and selection_allowed() and not params.get_bool(RETRY))
    self.refresh_btn.set_enabled(ui_state.is_offroad() and not busy)
    self.clear_cache_btn.set_enabled(ui_state.is_offroad() and not busy)
    if txn or status in ('ready', 'restored', 'cancelled'):
      # No modal switching screen and no forced interactive timeout: navigation
      # remains usable even if a model or GPU fails. Recovery runs in manager.
      self.current_model_info.current_model_header._shimmer = False
      self.current_model_info.info_header._shimmer = False
      self.current_model_info.current_model_header.set_text(tr('model switch'))
      self.current_model_info.current_model_text.set_text(STATUS_TEXT.get(status, status or ''))
      previous = (txn or {}).get('previous') or {}
      candidate = (txn or {}).get('candidate') or {}
      if txn:
        name = previous.get('displayName', 'Default') if txn['restoring'] else candidate.get('displayName', 'Default')
        self.current_model_info.info_header.set_text(name.lower())
        self.current_model_info.info_text.set_text(params.get(ERROR) or tr('drive manually'))
