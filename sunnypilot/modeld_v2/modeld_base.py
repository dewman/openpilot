"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""


class ModelStateBase:
  def __init__(self):
    # BluePilot: the owner resolves timing after obtaining the current CarParams.
    self.lat_delay = 0.0
    # End BluePilot
