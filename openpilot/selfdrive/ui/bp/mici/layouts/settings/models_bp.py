"""BluePilot Models: cached onroad selection and nonmodal, bounded handover status."""
import time

from openpilot.cereal import custom
from openpilot.bluepilot.models.cache import pinned_favorites, offline_status
from openpilot.bluepilot.models.warmup import switch_ready_bundle
from openpilot.bluepilot.models.switch import STATUS, ERROR, TRANSACTION, RETRY, STATUS_TEXT, stage_bundle, switch_busy
from openpilot.selfdrive.ui.mici.widgets.button import BigButton
from openpilot.selfdrive.ui.mici.widgets.dialog import BigDialog
from openpilot.selfdrive.ui.bp.mici.widgets.button_bp import BigParamControlBP
from openpilot.selfdrive.ui.bp.mici.widgets.floatbutton import BigParamFloatControl
from openpilot.selfdrive.ui.sunnypilot.mici.layouts.models import ModelsLayoutMici
from openpilot.selfdrive.ui.ui_state import ui_state
from openpilot.system.ui.lib.application import gui_app
from openpilot.system.ui.lib.multilang import tr


def selection_block_reason():
  if ui_state.is_offroad():
    return None
  sm = ui_state.sm
  names = ['selfdriveState', 'selfdriveStateSP', 'carState']
  now = time.monotonic()
  # UI polling is slower than the 100 Hz publishers. Their receive-rate average
  # cannot establish freshness here; use valid/alive data and source timestamps.
  if not (sm.all_alive(names) and sm.all_valid(names) and
          all(0 <= now - sm.logMonoTime[s] / 1e9 < 1 for s in names)):
    return tr('waiting for current vehicle state')
  if sm['carState'].cruiseState.enabled or sm['selfdriveState'].enabled:
    return tr('disengage cruise to switch')
  if sm['selfdriveStateSP'].mads.enabled:
    return tr('turn MADS steering off with the lane-centering button')
  return None


def selection_allowed():
  return selection_block_reason() is None


class SoftwareDelayParamControlBP(BigParamFloatControl):
  def _format_value(self, value):
    return super()._format_value(value) + ' s'


class ModelsLayoutMiciBP(ModelsLayoutMici):
  def __init__(self):
    super().__init__()
    self.retry_btn = BigButton(tr('retry previous model'))
    self.retry_btn.set_click_callback(self._retry_previous)
    self._scroller.add_widget(self.retry_btn)
    self.lagd_toggle = BigParamControlBP(tr('Live Learning Steer Delay'), 'LagdToggle')
    self.delay_control = SoftwareDelayParamControlBP(tr('Adjust Software Delay'), 'LagdToggleDelay', min=0.05, max=0.50, step=0.01)
    self._scroller.add_widgets([self.lagd_toggle, self.delay_control])
    # Keep driving controls next to selection, before offroad cache maintenance.
    for control in (self.lagd_toggle, self.delay_control):
      self._scroller.items.remove(control)
    self._scroller.items[2:2] = [self.lagd_toggle, self.delay_control]

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
      if entry['source'] == source and switch_ready_bundle(ui_state.params, bundle, source):
        btn = BigButton(bundle['displayName'].lower())
        btn.set_click_callback(lambda b=bundle: self._select_model(custom.ModelManagerSP.ModelBundle(**b)))
        buttons.append(btn)
    if buttons:
      self._push_selection_view(buttons)
    else:
      gui_app.push_widget(BigDialog(tr('no cached Favorites'), tr('Mark models as Favorites and let them download and prepare offroad first.')))

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
    self.select_model_btn.set_value(selection_block_reason() or offline_status(params))
    learning = params.get_bool('LagdToggle')
    self.lagd_toggle.set_checked(learning)
    self.delay_control.set_enabled(not learning)
    if learning:
      delay = ui_state.sm['lateralDelay'].lateralDelay
      self.lagd_toggle.set_value(tr('live delay') + f': {delay:.3f} s')
    else:
      self.lagd_toggle.set_value(tr('using fixed software delay'))
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
