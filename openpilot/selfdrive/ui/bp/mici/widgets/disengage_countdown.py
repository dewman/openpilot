"""Visible, cancellable warning before a UI action requests disengagement."""
import time
import pyray as rl

from openpilot.selfdrive.ui.mici.widgets.button import BigToggle
from openpilot.selfdrive.ui.mici.widgets.dialog import BigDialogBase
from openpilot.selfdrive.ui.ui_state import ui_state
from openpilot.system.ui.lib.application import gui_app, TextAlignment
from openpilot.system.ui.widgets.label import gui_label
from openpilot.system.ui.lib.multilang import tr


class DisengageCountdownBP(BigDialogBase):
  def __init__(self, action, clock=time.monotonic):
    super().__init__()
    self._action, self._clock = action, clock
    self._remaining = 3
    self._tick_time = None
    self._shown = False
    self.set_shown_callback(self._start)

  def _start(self):
    self._shown = True

  def _render(self, rect):
    width = rect.width - 40
    gui_label(rl.Rectangle(rect.x + 20, rect.y + rect.height * .12, width, rect.height * .2),
              tr('comma about to disengage'), font_size=32, alignment=TextAlignment.CENTER)
    gui_label(rl.Rectangle(rect.x + 20, rect.y + rect.height * .32, width, rect.height * .38),
              str(max(1, self._remaining)), font_size=92, alignment=TextAlignment.CENTER, color=rl.ORANGE)
    gui_label(rl.Rectangle(rect.x + 20, rect.y + rect.height * .74, width, rect.height * .15),
              tr('take control • swipe down to cancel'), font_size=24, alignment=TextAlignment.CENTER)

  def _update_state(self):
    super()._update_state()
    if not gui_app.widget_in_stack(self) or self.is_dismissing:
      return
    if not self.enabled or not self.is_visible:
      # Never count time while another screen covers this warning.
      self._remaining, self._tick_time = 3, None
      return
    if not self._shown:
      return
    now = self._clock()
    if self._tick_time is None:
      self._tick_time = now
    if now - self._tick_time >= 1:
      # Advance only one digit per rendered interval, even after a UI stall.
      self._remaining -= 1
      self._tick_time = now
      if self._remaining == 0:
        self.dismiss(self._action)


def warn_before_disengage(action):
  if ui_state.is_offroad():
    action()
  else:
    gui_app.push_widget(DisengageCountdownBP(action))


class AssistanceToggleBP(BigToggle):
  """Defer the actual parameter write until the warning completes."""
  def __init__(self, text, param, toggle_callback, **kwargs):
    self._param, self._apply_callback = param, toggle_callback
    super().__init__(text, initial_state=ui_state.params.get_bool(param),
                     toggle_callback=self._request_change, **kwargs)

  def _request_change(self, enabled):
    # Keep showing the applied setting while the countdown can still be cancelled.
    self.set_checked(ui_state.params.get_bool(self._param))

    def apply():
      self._apply_callback(enabled)
      ui_state.params.put_bool(self._param, enabled, block=True)
      self.set_checked(enabled)

    if enabled:
      apply()
    else:
      warn_before_disengage(apply)
