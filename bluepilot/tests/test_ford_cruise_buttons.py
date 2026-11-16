"""Real Ford CAN button edges must unlock only a deliberate post-switch press."""
from types import SimpleNamespace

import pytest

from opendbc.can import CANPacker
from opendbc.car import Bus, structs
from opendbc.car.ford.carstate import CarState
from opendbc.car.ford.values import CAR, FordFlags
from openpilot.bluepilot.models.engagement import ModelSwitchEngagementBP
from openpilot.bluepilot.models.switch import TRANSACTION, REENGAGE
from openpilot.bluepilot.tests.test_model_switch import Params


@pytest.mark.parametrize('signal,button', [
  ('CcAslButtnSetPress', 'setCruise'),
  ('CcAslButtnSetDecPress', 'setCruise'),
  ('CcAslButtnSetIncPress', 'setCruise'),
  ('CcAsllButtnResPress', 'resumeCruise'),
  ('CcAslButtnResIncPress', 'resumeCruise'),
  ('CcAslButtnResDecPress', 'resumeCruise'),
])
def test_physical_press_after_unlock_is_required(signal, button):
  cp = structs.CarParams(carFingerprint=CAR.FORD_F_150_MK14, flags=int(FordFlags.CANFD),
                        transmissionType=structs.CarParams.TransmissionType.automatic,
                        safetyConfigs=[structs.CarParams.SafetyConfig()])
  cp_sp = structs.CarParamsSP()
  state = CarState(cp, cp_sp)
  parsers = state.get_can_parsers(cp, cp_sp)
  packer = CANPacker(parsers[Bus.pt].dbc_name)
  params = Params()
  latch = ModelSwitchEngagementBP(params)
  events = SimpleNamespace(add=lambda event: None)
  now = 1_000_000_000

  def sample(pressed):
    nonlocal now
    now += 20_000_000
    frame = packer.make_can_msg('Steering_Data_FD1', parsers[Bus.pt].bus, {signal: pressed})
    for parser in parsers.values():
      parser.update([now, [frame]])
    cs, _ = state.update(parsers)
    latch.update(cs, events)
    return cs

  params.put(TRANSACTION, {'id': 'switch'})
  sample(False)
  cs = sample(True)
  assert [(str(b.type), b.pressed) for b in cs.buttonEvents] == [(button, True)]
  assert latch.blocked
  params.remove(TRANSACTION)
  # A button held through the switch cannot re-engage assistance.
  assert not sample(True).buttonEvents
  assert latch.blocked
  assert sample(False).buttonEvents[0].pressed is False
  assert latch.blocked
  cs = sample(True)
  assert cs.buttonEvents[0].pressed
  assert not latch.blocked
  assert not params.get_bool(REENGAGE)
  assert not sample(True).buttonEvents
