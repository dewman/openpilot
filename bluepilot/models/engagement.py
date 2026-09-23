"""Selfdrived side of the model-switch interlock, shared by stock and MADS."""
from cereal import car, log
from openpilot.bluepilot.models.switch import TRANSACTION, REENGAGE

Button = car.CarState.ButtonEvent.Type
ENGAGE_BUTTONS = (Button.accelCruise, Button.resumeCruise, Button.decelCruise, Button.setCruise, Button.lkas)


class ModelSwitchEngagementBP:
  def __init__(self, params):
    self.params = params
    self.token = ""
    self.wait_for_button = params.get_bool(REENGAGE)
    self.held = set()
    self.released_after_unlock = False

  def update(self, CS, events):
    """Run before either engagement state machine, including initialization."""
    transaction = self.params.get(TRANSACTION)
    self.token = transaction['id'] if transaction is not None else ""
    for button in CS.buttonEvents:
      if button.type in ENGAGE_BUTTONS:
        if button.pressed:
          self.held.add(button.type)
        else:
          self.held.discard(button.type)

    if transaction is not None:
      self.wait_for_button = True
      self.released_after_unlock = False
      events.add(log.OnroadEvent.EventName.bpModelSwitch)
    elif self.wait_for_button:
      if self.released_after_unlock and any(b.pressed and b.type in ENGAGE_BUTTONS for b in CS.buttonEvents):
        self.wait_for_button = False
        self.params.put_bool(REENGAGE, False)
      else:
        events.add(log.OnroadEvent.EventName.bpModelSwitchReady)
      if not self.held:
        self.released_after_unlock = True
