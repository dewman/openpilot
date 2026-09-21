"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
from cereal import log

from opendbc.car import structs
from openpilot.common.params import Params
# BluePilot: share fixed/live selection with the consumers.
from openpilot.sunnypilot.livedelay.helpers import get_lat_delay
# End BluePilot


class LagdToggle:
  def __init__(self, CP: structs.CarParams):
    self.CP = CP
    self.params = Params()
    self.lag = 0.0

  def update(self, lag_msg: log.LiveDelayData) -> None:
    # BluePilot: cache is compatibility output only, never the source of selected timing.
    self.lag = get_lat_delay(self.params, lag_msg.liveDelay.lateralDelay, self.CP.steerActuatorDelay)
    self.params.put("LagdValueCache", self.lag)
    # End BluePilot
