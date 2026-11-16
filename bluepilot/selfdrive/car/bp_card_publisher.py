"""
BluePilot: Publishes BP-specific messages from card.py.

Centralizes controllerStateBP and carStateBP message publishing so that
card.py only needs a single function call instead of inline BP blocks.
"""

import time

import openpilot.cereal.messaging as messaging
from openpilot.cereal import custom
from openpilot.common.params import Params

_SETTINGS_INTERVAL = 5.0  # re-read params at most every 5 s
_settings_last_read: float | None = None
_settings_cache: dict = {}


def _get_bool(p: Params, key: str, default: bool = False) -> bool:
  try:
    return p.get_bool(key)
  except Exception:
    return default


def _get_float(p: Params, key: str, default: float = 0.0) -> float:
  try:
    v = p.get(key, return_default=True)
    if v in (None, b"", ""):
      return default
    if isinstance(v, bytes):
      v = v.decode("utf-8", errors="replace").strip("\x00")
    return float(v)
  except Exception:
    return default


def _get_int(p: Params, key: str, default: int = 0) -> int:
  try:
    v = p.get(key, return_default=True)
    if v in (None, b"", ""):
      return default
    if isinstance(v, bytes):
      v = v.decode("utf-8", errors="replace").strip("\x00")
    return max(0, min(255, int(float(v))))
  except Exception:
    return default


def _get_str(p: Params, key: str, default: str = "") -> str:
  try:
    v = p.get(key, return_default=True)
    if v is None:
      return default
    return v.decode("utf-8", errors="replace").strip("\x00") if isinstance(v, bytes) else str(v).strip()
  except Exception:
    return default


def _refresh_settings_cache() -> dict:
  """BluePilot-menu settings snapshot -- see custom.capnp ControllerStateBP for the field-by-field
  param-key mapping and the field-retirement convention. Keep this dict's keys in sync with the
  capnp/structs.py bms* field names (1:1, same order, same grouping comments)."""
  p = Params()
  return {
    # --- System ---
    "bmsUiDebugLogging":               _get_bool(p, "BPUIDebugLog"),
    "bmsConnectBackend":               _get_int(p, "BPConnectBackend", 0),
    "bmsWebRoutesServerEnabled":       _get_bool(p, "EnableWebRoutesServer"),
    "bmsPreferredWifiNetwork":         _get_str(p, "WifiFavoriteSSID"),
    # --- Vehicle ---
    "bmsShowBlueCruiseUiOnCluster":    _get_bool(p, "send_hands_free_cluster_msg"),
    "bmsTwelveVBatteryLimit":          _get_float(p, "vbatt_pause_charging", 11.8),
    # --- Visuals ---
    "bmsHideOnroadBorder":             _get_bool(p, "BPHideOnroadBorder"),
    "bmsDisableLaneLineStatusColor":   _get_bool(p, "BPDisableLaneLineStatusColor"),
    "bmsMinimalDrivingView":           _get_bool(p, "BPHideCameraView"),
    "bmsEightBitRacerTheme":           _get_str(p, "BPThemePack").strip().lower() == "rad_racer",
    "bmsRainbowLaneLines":             _get_bool(p, "BPRainbowLines"),
    "bmsShowBlindspotOverlay":         _get_bool(p, "ShowBlindspotOverlay", True),
    "bmsShowBrakeStatus":              _get_bool(p, "ShowBrakeStatus"),
    "bmsShowConfidenceBall":           _get_bool(p, "BPShowConfidenceBall", True),
    "bmsAnimateSteeringWheel":         _get_bool(p, "BPAnimateSteeringWheel", True),
    "bmsWheelIconStyle":               _get_int(p, "BPSteeringWheelIconStyle", 0),
    "bmsShowRadarLeadOverlay":         _get_bool(p, "FordPrefShowRadarLeadOverlay", True),
    "bmsRadarOverlaySize":             _get_int(p, "FordPrefRadarOverlaySize", 1),
    "bmsShowHybridBatteryStatus":      _get_bool(p, "FordPrefHybridBatteryStatus"),
    "bmsShowHybridPowerFlow":          _get_bool(p, "FordPrefHybridPowerFlow"),
    "bmsHybridDriveGaugeSize":         _get_int(p, "FordPrefHybridDriveGaugeSize", 1),
    "bmsHybridGaugeStyle":             _get_int(p, "FordPrefGaugeStyle", 0),
    "bmsHybridPowerFlowStyleRound":    _get_bool(p, "FordPrefHybridPowerFlowAlternate"),
    "bmsLowerRightDisplay":            _get_int(p, "mici_complication", 0),
    "bmsRainbowMode":                  _get_bool(p, "RainbowMode"),
    "bmsHideOnroadFade":               _get_bool(p, "mici_hide_onroad_fade"),
    # --- Longitudinal Tuning ---
    "bmsBypassBpLongitudinalControl":  _get_bool(p, "disable_BP_long_UI"),
    "bmsDisableDownhillCompensation":  _get_bool(p, "disable_downhill_comp_UI"),
    "bmsDisableFordRadarVisionOnly":   _get_bool(p, "disable_ford_radar_UI"),
    # --- Lateral Tuning ---
    "bmsDisableBpLateralControl":      _get_bool(p, "disable_BP_lat_UI"),
    "bmsPrimaryControlVariable":       _get_int(p, "FordPrefLateralControl", 0),
    "bmsDisableLaneChangeUnderSpeed":  _get_bool(p, "BlinkerPauseLaneChange"),
    "bmsMinimumSpeedToPauseLaneChange": _get_int(p, "BlinkerMinLateralControlSpeed", 20),
    "bmsShowLateralControlMode":       _get_bool(p, "BpShowLateralControl"),
    # --- Angle Tuning ---
    "bmsLowSpeedAdjustmentFactor":     _get_float(p, "FordLowSpeedFactor_ang", 1.0),
    "bmsHighSpeedAdjustmentFactor":    _get_float(p, "FordHighSpeedFactor_ang", 1.0),
    "bmsLaneChangeFactorHighAngle":    _get_float(p, "lane_change_factor_high_ang", 1.0),
    # --- Curvature Tuning ---
    "bmsEnableHumanTurnDetection":     _get_bool(p, "enable_human_turn_detection_curv", True),
    "bmsLaneChangeFactorHighCurvature": _get_float(p, "lane_change_factor_high_curv", 0.85),
    "bmsEnableLanePositioning":        _get_bool(p, "enable_lane_positioning_curv"),
    "bmsInLaneOffset":                 _get_float(p, "custom_path_offset_curv", 0.0),
    "bmsEnableLanefullMode":           _get_bool(p, "enable_lane_full_mode_curv"),
    "bmsUseCustomTuningProfile":       bool(_get_int(p, "custom_profile_curv", 0)),
    "bmsPredictedCurvatureBlendRatioHigh": _get_float(p, "pc_blend_ratio_high_C_UI_curv", 0.4),
    "bmsPredictedCurvatureBlendRatioLow":  _get_float(p, "pc_blend_ratio_low_C_UI_curv", 0.4),
    "bmsCenteringPidGain":             _get_float(p, "LC_PID_gain_UI_curv", 3.0),
  }


def refresh_controller_settings_bp():
  """Called by card's existing parameter thread; never perform Params I/O on its control loop."""
  global _settings_last_read, _settings_cache
  now = time.monotonic()
  if _settings_last_read is not None and now - _settings_last_read < _SETTINGS_INTERVAL:
    return
  try:
    # One assignment publishes a complete snapshot; readers retain the old dict
    # while this thread is reading, including when a Params call blocks or fails.
    _settings_cache = _refresh_settings_cache()
  except Exception:
    pass
  _settings_last_read = now


def publish_controller_state_bp(CI, pm):
  """Publish the extracted lateral controller's status without changing control."""
  status = getattr(getattr(CI.CC, "bp", None), "status", None)
  lateral_status = status() if status is not None else CI.CC
  if hasattr(lateral_status, "lateralUncertainty"):
    cs_bp = custom.ControllerStateBP.new_message()
    cs_bp.lateralUncertainty = lateral_status.lateralUncertainty
    cs_bp.angleRateLimited = getattr(lateral_status, "angleRateLimited", False)
    cs_bp.curvatureRateLimited = getattr(lateral_status, "curvatureRateLimited", False)
    cs_bp.curvatureDeviationLimited = getattr(lateral_status, "curvatureDeviationLimited", False)
    cs_bp.humanTurnLateralPaused = bool(getattr(lateral_status, "humanTurnLateralPaused", False))
    cs_bp.stallBlipActive = bool(getattr(lateral_status, "stallBlipActive", False))
    # BluePilot: mode the controller actually ran, straight off the car controller (not Params).
    if getattr(lateral_status, "disable_BP_lat_UI", True):
      cs_bp.activeLateralMode = custom.ControllerStateBP.LateralMode.openpilot
    elif getattr(lateral_status, "primary_lateral_control", 0) == 1:
      cs_bp.activeLateralMode = custom.ControllerStateBP.LateralMode.angle
    else:
      cs_bp.activeLateralMode = custom.ControllerStateBP.LateralMode.curvature

    # The background parameter thread publishes complete cached snapshots.
    # No Params reads, worker startup or waits are allowed on this control path.
    settings = _settings_cache
    for field, value in settings.items():
      setattr(cs_bp, field, value)

    # BluePilot: fingerprint info -- plain attribute reads on CarParams, no Params round-trip
    # needed, so no caching required (fingerprint never changes after startup).
    CP = getattr(CI, "CP", None)
    if CP is not None:
      cs_bp.bmsFingerprintForced = bool(getattr(CP, "fingerprintSource", None) == "fixed")
      cs_bp.bmsFingerprint = str(getattr(CP, "carFingerprint", "") or "")

    cs_bp_send = messaging.new_message('controllerStateBP')
    cs_bp_send.valid = True
    cs_bp_send.controllerStateBP = cs_bp
    pm.send('controllerStateBP', cs_bp_send)


def publish_car_state_bp(CI, pm, can_valid):
  """Publish carStateBP (hybrid drive gauge data) if available from car state."""
  if hasattr(CI.CS, 'car_state_bp_msg') and CI.CS.car_state_bp_msg is not None:
    cs_bp_send = CI.CS.car_state_bp_msg
    cs_bp_send.valid = can_valid
    pm.send('carStateBP', cs_bp_send)
