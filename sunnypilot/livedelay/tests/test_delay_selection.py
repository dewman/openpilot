"""Selected timing must not depend on a cache from another mode or vehicle."""
import math

import pytest

from openpilot.sunnypilot.livedelay.helpers import read_delay_settings, select_lat_delay, get_lat_delay


class ParamsStub:
  def __init__(self, learned=True, software=0.2):
    self.learned = learned
    self.software = software

  def get_bool(self, key):
    assert key == "LagdToggle"
    return self.learned

  def get(self, key, **kwargs):
    assert key == "LagdToggleDelay", "Control must never read LagdValueCache"
    return self.software


def test_live_fixed_live_transition_ignores_cache():
  params = ParamsStub()
  assert get_lat_delay(params, .338, .22) == .338
  params.learned = False
  params.software = .10
  selected = select_lat_delay(params, .338, .22)
  assert selected.source == "fixed"
  assert selected.value == pytest.approx(.32)
  params.software = .30
  assert get_lat_delay(params, .338, .22) == pytest.approx(.52)
  params.learned = True
  assert get_lat_delay(params, .331, .22) == .331


@pytest.mark.parametrize("live", [0., -1., math.nan, math.inf, -math.inf])
def test_startup_fallback_uses_current_vehicle(live):
  selected = select_lat_delay(ParamsStub(), live, .22)
  assert selected.source == "fallback"
  assert selected.value == pytest.approx(.42)
  assert get_lat_delay(ParamsStub(), live, .40) == pytest.approx(.60)


@pytest.mark.parametrize("software, expected", [
  (.10, .32), (b"0.15", .37), ("0.30", .52), (None, .42), ("", .42),
  ("bad", .42), (math.nan, .42), (math.inf, .42), (-.1, .27), (10., .72),
])
def test_fixed_software_validation(software, expected):
  selected = select_lat_delay(ParamsStub(False, software), .338, .22)
  assert selected.source == "fixed"
  assert selected.value == pytest.approx(expected)


def test_polled_settings_keep_source_and_value_together():
  params = ParamsStub(False, .10)
  settings = read_delay_settings(params)
  params.learned = True
  assert settings.select(.338, .22).source == "fixed"
  assert settings.select(.338, .22).value == pytest.approx(.32)
  assert read_delay_settings(params).select(.338, .22).value == .338
