"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""

# BluePilot: display-only Ford HEV text, independent of the vehicle parser.

def get_hev_power_flow_text(mode_value):
  """Convert HEV power flow mode value to human-readable text.

  These values come from the Cluster_HEV_Data2 CAN message (PwrFlowTxt_D_Dsply signal).
  """
  power_flow_modes = {
    0: "",
    1: "Hybrid Drive",
    2: "Charging HV Battery",
    3: "Idle",
    4: "Idle with Charging",
    5: "Electric Drive",
    6: "Engine Drive",
    7: "Remote Start",
    8: "Charge Complete",
    9: "Fast Charge Complete",
    10: "Fast Charging",
    11: "Regen Braking",
    12: "Not Used",
    13: "Not Used",
    14: "Not Used",
    15: "Not Used",
  }
  return power_flow_modes.get(int(mode_value), "Unknown")

def get_hev_engine_on_reason_text(reason_value):
  """Convert HEV engine-on reason value to human-readable text.

  These values come from the Cluster_HEV_Data2 CAN message (EngOnMsg1_D_Dsply signal).
  """
  engine_on_reasons = {
    0: "",
    1: "Acceleration",
    2: "High Speed",
    3: "Heater Setting",
    4: "Neutral Gear",
    5: "Engine Cold",
    6: "Battery Charging",
    7: "Low Gear",
    8: "Normal Operation",
    9: "Oil Maintenance",
    10: "Fuel Maintenance",
    11: "Hill Descent Control",
    12: "Battery Temperature",
    13: "Drive Mode",
  }
  return engine_on_reasons.get(int(reason_value), "Unknown")
