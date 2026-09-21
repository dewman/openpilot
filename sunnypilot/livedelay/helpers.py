"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
# BluePilot: resolve the selected delay directly; the asynchronous cache is not a control input.
import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
  from openpilot.common.params import Params


@dataclass(frozen=True)
class DelaySelection:
  value: float
  source: str  # live (including lagd's own fallback), fixed, or startup fallback


@dataclass(frozen=True)
class DelaySettings:
  learned: bool = True
  software_delay: float = 0.2

  def select(self, live_delay: float, actuator_delay: float) -> DelaySelection:
    """LiveDelay includes lagd's fallback; zero/nonfinite denotes unavailable startup data."""
    if self.learned:
      if math.isfinite(live_delay) and live_delay > 0.0:
        return DelaySelection(float(live_delay), "live")
      return DelaySelection(float(actuator_delay + 0.2), "fallback")
    return DelaySelection(float(actuator_delay + self.software_delay), "fixed")


def read_delay_settings(params: "Params") -> DelaySettings:
  """Read one mode snapshot; validate fixed software seconds against the UI range."""
  learned = params.get_bool("LagdToggle")
  if learned:
    return DelaySettings()
  try:
    software_delay = float(params.get("LagdToggleDelay", return_default=True))
    if not math.isfinite(software_delay):
      raise ValueError("Nonfinite software delay")
  except (TypeError, ValueError):
    software_delay = 0.2
  return DelaySettings(False, min(0.50, max(0.05, software_delay)))


def select_lat_delay(params: "Params", live_delay: float, actuator_delay: float) -> DelaySelection:
  """Resolve from current settings and vehicle, never a previous mode/vehicle's cache."""
  return read_delay_settings(params).select(live_delay, actuator_delay)


def get_lat_delay(params: "Params", stock_lat_delay: float, actuator_delay: float) -> float:
  """Return the selected delay for consumers that do not publish source diagnostics."""
  return select_lat_delay(params, stock_lat_delay, actuator_delay).value
# End BluePilot
