"""Verify the complete preference-to-packed-cluster-message path without a vehicle."""
from collections import defaultdict
from types import SimpleNamespace

import pytest

from opendbc.can import CANPacker
from opendbc.can.dbc import DBC
from opendbc.car import gen_empty_fingerprint, structs
from opendbc.car.ford import fordcan
from opendbc.car.ford.interface import CarInterface
from opendbc.car.ford.values import CAR, FordFlags
from openpilot.cereal import custom
from openpilot.selfdrive.car.helpers import convert_carControlSP
from openpilot.sunnypilot.selfdrive.controls.controlsd_ext import ControlsExt


def entries(enabled):
  controls = ControlsExt.__new__(ControlsExt)
  controls.params = SimpleNamespace(get=lambda key, **kw: enabled if key == 'send_hands_free_cluster_msg' else None)
  controls._bp_unknown_keys_logged = set()
  return ControlsExt.read_lateral_params(controls)


@pytest.mark.parametrize('canfd,enabled,preference,left,right,expected', [
  (True, True, True, False, False, 7),
  (True, True, False, False, False, 2),
  (True, False, True, False, False, 1),
  (False, True, True, False, False, 2),
  (True, True, True, True, False, 3),
  (True, True, True, False, True, 4),
])
def test_cluster_ui_scope_and_warning_passthrough(canfd, enabled, preference, left, right, expected):
  stock = defaultdict(int, TjaWarn_D_Rq=3, TjaMsgTxt_D_Dsply=1, FcwAudioWarn_B_Rq=1)
  hud = SimpleNamespace(leftLaneDepart=left, rightLaneDepart=right)
  cp = SimpleNamespace(flags=int(FordFlags.CANFD) if canfd else 0, openpilotLongitudinalControl=False)
  msg = fordcan.create_acc_ui_msg(CANPacker('ford_lincoln_base_pt'), fordcan.CanBus(fingerprint=gen_empty_fingerprint()), cp, True,
                                 enabled, False, False, False, hud, stock, bluecruise_ui=preference)
  signals = DBC('ford_lincoln_base_pt').msgs[msg[0]].sigs
  assert signals['Tja_D_Stat'].get_raw_value(msg[1]) == expected
  assert signals['TjaWarn_D_Rq'].get_raw_value(msg[1]) == 3
  assert signals['TjaMsgTxt_D_Dsply'].get_raw_value(msg[1]) == 1
  assert signals['FcwAudioWarn_B_Rq'].get_raw_value(msg[1]) == 1


def controller_messages(preference):
  fingerprint = gen_empty_fingerprint()
  fingerprint[0][0x5A] = 8
  cp = CarInterface.get_params(CAR.FORD_F_150_MK14, fingerprint, [], False, False, False)
  cp_sp = CarInterface.get_params_sp(cp, cp.carFingerprint, fingerprint, [], False, False, False)
  interface = CarInterface(cp, cp_sp)
  interface.update([])
  control = structs.CarControl(enabled=True, latActive=True).as_reader()
  control_sp = convert_carControlSP(custom.CarControlSP.new_message(params=entries(preference)).as_reader())
  _, messages = interface.apply(control, control_sp, 0)
  return messages


@pytest.mark.parametrize('preference,expected', [(False, 2), (True, 7)])
def test_preference_reaches_real_ford_controller(preference, expected):
  messages = controller_messages(preference)
  cluster = next(m for m in messages if m[0] == 0x18A)
  signal = DBC('ford_lincoln_base_pt').msgs[0x18A].sigs['Tja_D_Stat']
  assert signal.get_raw_value(cluster[1]) == expected


def test_cluster_preference_does_not_change_other_can_commands():
  disabled, enabled = controller_messages(False), controller_messages(True)
  assert [m for m in disabled if m[0] != 0x18A] == [m for m in enabled if m[0] != 0x18A]
  off = next(m[1] for m in disabled if m[0] == 0x18A)
  on = next(m[1] for m in enabled if m[0] == 0x18A)
  # Only the three-bit TJA display field changes from 2 to 7.
  assert bytes(a ^ b for a, b in zip(off, on, strict=True)) == bytes([0, 0, 0, 0, 0, 0x14, 0, 0])
