"""A startup status burst must not invalidate healthy one-Hz Ford camera CAN."""
from types import SimpleNamespace

from opendbc.car import Bus
from opendbc.car.ford.carstate import CarState
from opendbc.car.ford.values import CAR
from opendbc.can import CANPacker


def test_ipma_burst_then_one_hz_and_genuine_timeout():
  cp = SimpleNamespace(carFingerprint=CAR.FORD_F_150_MK14, flags=0, safetyConfigs=[None])
  parser = CarState.get_can_parsers(cp, None)[Bus.cam]
  packer = CANPacker(parser.dbc_name)
  bus = parser.bus
  # Keep the camera bus alive independently to distinguish one missing message
  # from a whole-bus timeout. IPMA still must become invalid if it disappears.
  for step in range(1520):
    now = 1_000_000_000 + step * 10_000_000
    frames = [(0x123, b'\x00' * 8, bus)]
    if (step < 150 and step % 5 == 0) or (150 <= step <= 500 and step % 100 == 0):
      addr, data, _ = packer.make_can_msg('IPMA_Data', bus, {})
      frames.append((addr, data, bus))
    parser.update([now, frames])
    valid = parser.can_valid
    if 1 <= step <= 500:
      assert valid
  assert not parser.bus_timeout
  # Last IPMA was at 6 seconds; ten seconds without it must fail.
  assert not valid
