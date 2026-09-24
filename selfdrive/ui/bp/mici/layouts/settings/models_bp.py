"""BluePilot comma 4 Models menu with the shared steering-delay settings."""
from collections.abc import Callable

from openpilot.selfdrive.ui.bp.mici.widgets.button_bp import BigParamControlBP
from openpilot.selfdrive.ui.bp.mici.widgets.floatbutton import BigParamFloatControl
from openpilot.selfdrive.ui.sunnypilot.mici.layouts.models import ModelsLayoutMici
from openpilot.selfdrive.ui.ui_state import ui_state
from openpilot.system.ui.lib.multilang import tr


class SteerDelayControlBP(BigParamFloatControl):
  def __init__(self):
    super().__init__(tr("Adjust Steer Delay"), "LagdToggleDelay", min=0.05, max=0.50, step=0.01)

  def _format_value(self, value: float) -> str:
    return tr("{delay} s software").format(delay=f"{value:.2f}")


class ModelsLayoutMiciBP(ModelsLayoutMici):
  def __init__(self, back_callback: Callable):
    super().__init__(back_callback)
    self.lagd_toggle = BigParamControlBP(tr("Live Learning Steer Delay"), "LagdToggle")
    self.delay_control = SteerDelayControlBP()
    delay_items = [self.lagd_toggle, self.delay_control]
    self.main_items.extend(delay_items)
    self._scroller.add_widgets(delay_items)
    self._update_delay_controls()

  def _update_delay_controls(self):
    # Re-read the same Params used by SunnyLink so remote changes appear here too.
    learned = ui_state.params.get_bool("LagdToggle")
    self.lagd_toggle.set_checked(learned)
    self.delay_control.set_enabled(not learned)

  def _update_state(self):
    super()._update_state()
    self._update_delay_controls()
