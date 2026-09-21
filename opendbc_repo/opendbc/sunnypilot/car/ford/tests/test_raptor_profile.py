"""Raptor model identity, complete runtime geometry, and unchanged F-150 control paths."""
import pytest

from opendbc.car import gen_empty_fingerprint, scale_rot_inertia, scale_tire_stiffness
from opendbc.car.ford.interface import CarInterface
from opendbc.car.ford.values import CAR, DBC, FordFlags, FordSafetyFlags, match_vin_to_car
from opendbc.car.ford.fingerprints import FW_VERSIONS
from opendbc.car.structs import CarParamsSP
from opendbc.car.vehicle_model import VehicleModel, calc_slip_factor
from opendbc.sunnypilot.car.interfaces import _initialize_ford
from opendbc.sunnypilot.car.ford.values_ext import FORD_PINION_GEOMETRY_INDEX, platform_gains
from opendbc.safety.tests.libsafety import libsafety_py


def raptor_params(alpha_long=False):
  fp = gen_empty_fingerprint()
  fp[0][0x5A] = 8  # automatic gearbox
  return CarInterface.get_params(CAR.FORD_F_150_RAPTOR_MK3, fp, [], alpha_long, False, False)


@pytest.mark.parametrize("alpha_long", [False, True])
def test_runtime_geometry_and_safety(alpha_long):
  cp = raptor_params(alpha_long)
  assert cp.mass == 2747  # 2611 kg published base curb weight plus the standard 136 kg payload
  assert cp.wheelbase == pytest.approx(3.694)
  assert cp.steerRatio == pytest.approx(17.2)
  assert cp.centerToFront == pytest.approx(cp.wheelbase * .44)
  assert cp.rotationalInertia == pytest.approx(scale_rot_inertia(cp.mass, cp.wheelbase))
  assert (cp.tireStiffnessFront, cp.tireStiffnessRear) == pytest.approx(
    scale_tire_stiffness(cp.mass, cp.wheelbase, cp.centerToFront, cp.tireStiffnessFactor))
  assert cp.steerActuatorDelay == pytest.approx(.22)
  assert cp.flags & FordFlags.CANFD
  assert not cp.dashcamOnly
  assert cp.openpilotLongitudinalControl == alpha_long
  assert cp.safetyConfigs[-1].safetyParam == int(FordSafetyFlags.CANFD | (FordSafetyFlags.LONG_CONTROL if alpha_long else 0))
  # Compare to the fully constructed runtime CP, including payload and the Ford CG convention.
  safety = libsafety_py.libsafety
  idx = FORD_PINION_GEOMETRY_INDEX[cp.carFingerprint]
  assert idx == 13
  assert safety.get_ford_pinion_geometry_wheelbase(idx) == pytest.approx(cp.wheelbase)
  assert safety.get_ford_pinion_geometry_steer_ratio(idx) == pytest.approx(cp.steerRatio)
  assert safety.get_ford_pinion_geometry_slip_factor(idx) == pytest.approx(calc_slip_factor(VehicleModel(cp)), rel=1e-5)
  for enabled, expected in (("0", 0), ("1", 27)):
    sp = CarParamsSP()
    _initialize_ford(cp, sp, {"FordPrefSteerAngleCurvature": enabled})
    assert sp.safetyParam == expected


def test_explicit_profile_does_not_change_generic_f150():
  generic = CarInterface.get_non_essential_params(CAR.FORD_F_150_MK14)
  assert (generic.mass, generic.wheelbase, generic.steerRatio) == pytest.approx((3470, 3.99, 17.0))
  assert DBC[CAR.FORD_F_150_RAPTOR_MK3] == DBC[CAR.FORD_F_150_MK14]
  assert platform_gains(CAR.FORD_F_150_RAPTOR_MK3) == platform_gains(CAR.FORD_F_150_MK14)
  assert CAR.FORD_F_150_RAPTOR_MK3 not in FW_VERSIONS
  assert not CAR.FORD_F_150_RAPTOR_MK3.config.vds_codes
  assert CAR.FORD_F_150_RAPTOR_MK3 not in match_vin_to_car("1FTFW1E85MFXXXXXX")
