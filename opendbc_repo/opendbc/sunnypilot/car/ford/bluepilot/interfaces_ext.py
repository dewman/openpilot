"""
BluePilot (full BluePilot only): Ford longitudinal mode selection, from BluePilot 7.

Every Ford gets the alpha longitudinal toggle, and the toggle decides: on = openpilot
longitudinal, off = the car's own ACC. Upstream only offers the toggle on Fords without a
radar in the DBC (CAN FD) and always runs openpilot longitudinal on the CAN Fords that have one.

Called at the end of CarInterface._get_params() in opendbc/car/ford/interface.py.
"""
from opendbc.car import structs
from opendbc.car.ford.values import FordFlags, FordSafetyFlags

# Safety param bit (ford.h): restrict a CAN Ford to the stock TX set (no ACCDATA), so the car's
# own ACC runs. Upstream's CAN Ford always gets the longitudinal set, so the bit is BluePilot's
# own and the top bit, as far as possible from where upstream adds Ford flags.
FORD_SAFETY_BP_STOCK_LONG = 0x8000


def apply_ford_ext_params(ret: structs.CarParams, alpha_long: bool) -> None:
  ret.alphaLongitudinalAvailable = True
  ret.openpilotLongitudinalControl = bool(alpha_long)

  safety = ret.safetyConfigs[-1]
  if ret.openpilotLongitudinalControl:
    safety.safetyParam |= FordSafetyFlags.LONG_CONTROL.value
  else:
    safety.safetyParam &= ~FordSafetyFlags.LONG_CONTROL.value
    if not ret.flags & FordFlags.CANFD:
      safety.safetyParam |= FORD_SAFETY_BP_STOCK_LONG
