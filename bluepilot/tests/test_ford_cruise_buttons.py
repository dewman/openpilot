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
  ('CcAslButtnCnclResPress', 'resumeCruise'),
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


@pytest.fixture
def shared_button_car():
  cp = structs.CarParams(carFingerprint=CAR.FORD_F_150_MK14, flags=int(FordFlags.CANFD),
                        transmissionType=structs.CarParams.TransmissionType.automatic,
                        safetyConfigs=[structs.CarParams.SafetyConfig()])
  state = CarState(cp, structs.CarParamsSP())
  parsers = state.get_can_parsers(cp, state.CP_SP)
  packer = CANPacker(parsers[Bus.pt].dbc_name)
  now = 1_000_000_000

  def sample(pressed=False, cruise=False, dedicated=False):
    nonlocal now
    now += 20_000_000
    frames = [packer.make_can_msg('Steering_Data_FD1', parsers[Bus.pt].bus, {
      'CcAslButtnCnclResPress': pressed, 'CcAsllButtnResPress': dedicated,
    }), packer.make_can_msg('EngBrakeData', parsers[Bus.pt].bus, {'CcStat_D_Actl': 4 if cruise else 3})]
    for parser in parsers.values():
      parser.update([now, frames])
    cs, _ = state.update(parsers)
    # Receiving the button does not alter the stock values forwarded by the controller.
    assert state.buttons_stock_values['CcAslButtnCnclResPress'] == pressed
    return cs

  return sample


def button_edges(cs):
  return [(str(b.type), b.pressed) for b in cs.buttonEvents]


@pytest.mark.parametrize('cruise,expected', [(False, 'resumeCruise'), (True, 'cancel')])
def test_shared_press_and_release_keep_type_through_cruise_change(shared_button_car, cruise, expected):
  sample = shared_button_car
  assert not sample(cruise=cruise).buttonEvents
  assert button_edges(sample(True, cruise)) == [(expected, True)]
  for _ in range(20):
    assert not sample(True, not cruise).buttonEvents
  assert button_edges(sample(False, not cruise)) == [(expected, False)]
  assert not sample(False, not cruise).buttonEvents
  # Only a fresh press can acquire the other meaning.
  other = 'cancel' if expected == 'resumeCruise' else 'resumeCruise'
  assert button_edges(sample(True, not cruise)) == [(other, True)]


def test_shared_and_dedicated_resume_do_not_duplicate_or_release_early(shared_button_car):
  sample = shared_button_car
  sample()
  assert button_edges(sample(True, dedicated=True)) == [('resumeCruise', True)]
  assert not sample(True, cruise=True, dedicated=False).buttonEvents
  assert not sample(True, cruise=True, dedicated=True).buttonEvents
  assert not sample(False, cruise=True, dedicated=True).buttonEvents
  assert button_edges(sample(False, cruise=True, dedicated=False)) == [('resumeCruise', False)]


def test_cancel_held_through_switch_cannot_become_resume(shared_button_car):
  sample = shared_button_car
  params = Params()
  params.put(TRANSACTION, {'id': 'switch'})
  latch = ModelSwitchEngagementBP(params)
  events = SimpleNamespace(add=lambda event: None)
  latch.update(sample(cruise=True), events)
  cs = sample(True, cruise=True)
  assert button_edges(cs) == [('cancel', True)]
  latch.update(cs, events)
  params.remove(TRANSACTION)
  for _ in range(10):
    cs = sample(True, cruise=False)
    assert not cs.buttonEvents
    latch.update(cs, events)
    assert latch.blocked
  cs = sample(False)
  assert button_edges(cs) == [('cancel', False)]
  latch.update(cs, events)
  assert latch.blocked
  cs = sample(True)
  assert button_edges(cs) == [('resumeCruise', True)]
  latch.update(cs, events)
  assert not latch.blocked


def test_fresh_cancel_does_not_clear_reengagement_latch(shared_button_car):
  sample = shared_button_car
  params = Params()
  params.put_bool(REENGAGE, True)
  latch = ModelSwitchEngagementBP(params)
  events = SimpleNamespace(add=lambda event: None)
  latch.update(sample(cruise=True), events)
  cs = sample(True, cruise=True)
  assert button_edges(cs) == [('cancel', True)]
  latch.update(cs, events)
  assert latch.blocked
