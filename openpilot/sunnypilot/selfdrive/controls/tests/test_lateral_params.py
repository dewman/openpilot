"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
from pathlib import Path
from collections import defaultdict
from unittest import mock

from openpilot.cereal import custom, messaging
from openpilot.common.params import Params
from openpilot.common.test import OpenpilotTestCase
from openpilot.sunnypilot.selfdrive.controls.controlsd_ext import ControlsExt
from opendbc.bluepilot_lateral.core.values_ext import BP_LATERAL_PARAMS
from opendbc.bluepilot_lateral.params.manifest import params_keys_lines

PARAMS_KEYS_H = Path(__file__).resolve().parents[4] / "common" / "params_keys.h"


def _published(params) -> dict[str, bytes]:
  ext = ControlsExt.__new__(ControlsExt)  # read_lateral_params only needs params and the log set
  ext.params = params
  ext._bp_unknown_keys_logged = set()
  return {e["key"]: e["value"] for e in ext.read_lateral_params()}


class _Written:
  """Params with some keys written. Writes inside the test's isolated params directory did not
  persist on a device, so written values are layered over Params instead of put()."""
  def __init__(self, params: Params, **written):
    self._params, self._written = params, written

  def get(self, key, return_default=False):
    return self._written[key] if key in self._written else self._params.get(key, return_default=return_default)


class TestReadLateralParams(OpenpilotTestCase):
  """BluePilot (#6): the Ford lateral gets the value the settings UI shows, including for a param
  that was never written on the device."""

  def setup_method(self):
    self.params = Params()
    for key in BP_LATERAL_PARAMS:
      self.params.remove(key)

  def test_unset_keys_publish_the_registry_default(self):
    published = _published(self.params)
    self.assertEqual(set(published), set(BP_LATERAL_PARAMS) | {"send_hands_free_cluster_msg"})
    self.assertEqual(published["enable_human_turn_detection_curv"], b"1")
    self.assertEqual(published["LC_PID_gain_UI_curv"], b"1.0")

  def test_decided_defaults(self):
    self.assertTrue(self.params.get("enable_human_turn_detection_curv", return_default=True))
    self.assertEqual(self.params.get("LC_PID_gain_UI_curv", return_default=True), 1.0)

  def test_written_value_wins(self):
    published = _published(_Written(self.params, LC_PID_gain_UI_curv=2.5, enable_human_turn_detection_curv=False))
    self.assertEqual(published["LC_PID_gain_UI_curv"], b"2.5")
    self.assertEqual(published["enable_human_turn_detection_curv"], b"0")


def _bluepilot_block(text: str) -> set[str]:
  """The entries under params_keys.h's BluePilot comment, up to the next blank line or the map's end."""
  lines = text.splitlines()
  start = next(i for i, line in enumerate(lines) if line.strip().startswith("// BluePilot Ford lateral control"))
  block = set()
  for line in lines[start + 1:]:
    entry = line.strip()
    if not entry or entry == "};":
      break
    if entry.startswith("{"):
      block.add(" ".join(entry.split()))
  return block


class TestParamsKeysMatchManifest(OpenpilotTestCase):
  """BluePilot's params_keys.h entries are the library manifest's, both ways: every setting registered
  with its type and default, and nothing the manifest dropped."""

  def test_block_matches_manifest(self):
    block = _bluepilot_block(PARAMS_KEYS_H.read_text())
    manifest = {" ".join(line.split()) for line in params_keys_lines()}
    self.assertEqual(block, manifest, "paste the lines `python -m opendbc.bluepilot_lateral.params.manifest` prints")


class _CountingParams(_Written):
  def __init__(self, params, **written):
    super().__init__(params, **written)
    self.calls = []

  def get(self, key, return_default=False):
    self.calls.append(key)
    return super().get(key, return_default=return_default)


class _SM(dict):
  def __init__(self):
    super().__init__({s: getattr(messaging.new_message(s), s)
                     for s in ('radarState', 'selfdriveStateSP', 'modelV2', 'vehicleParameters', 'lateralDelay')})
    self.seen = defaultdict(bool)


class TestPublishedParamsCache(OpenpilotTestCase):
  def ext(self, params, required=True):
    ext = ControlsExt.__new__(ControlsExt)
    ext.params = params
    ext._bp_unknown_keys_logged = set()
    ext.CP_SP = custom.CarParamsSP.new_message(lateralInputsRequired=required)
    return ext

  def test_first_frame_preserves_all_nineteen_settings_and_encoding(self):
    params = _CountingParams(Params(), LC_PID_gain_UI_curv=2.5, enable_human_turn_detection_curv=False,
                             send_hands_free_cluster_msg=True)
    ext = self.ext(params)
    expected = ext.read_lateral_params()
    params.calls.clear()
    with mock.patch('openpilot.sunnypilot.selfdrive.controls.controlsd_ext.time.monotonic', return_value=0.0):
      actual = ext.state_control_ext(_SM())
    values = [{'key': p.key, 'value': p.value} for p in actual.params]
    self.assertEqual(values, expected)
    self.assertEqual(len(values), 19)
    self.assertEqual(params.calls, [*BP_LATERAL_PARAMS, 'send_hands_free_cluster_msg'])
    self.assertEqual({p['key']: p['value'] for p in values}['send_hands_free_cluster_msg'], b'1')

  def test_refresh_timing_includes_bypass_and_cluster_preferences(self):
    params = _CountingParams(Params(), disable_BP_lat_UI=False, send_hands_free_cluster_msg=False)
    ext = self.ext(params)
    sm = _SM()
    with mock.patch('openpilot.sunnypilot.selfdrive.controls.controlsd_ext.time.monotonic', return_value=0.0) as clock:
      initial = ext.state_control_ext(sm).to_bytes()
      params._written.update(disable_BP_lat_UI=True, send_hands_free_cluster_msg=True)
      # Upstream's strict > comparison refreshes on the first frame after 0.5 s.
      for frame in range(1, 51):
        clock.return_value = frame / 100
        self.assertEqual(ext.state_control_ext(sm).to_bytes(), initial)
      self.assertEqual(len(params.calls), 19)
      clock.return_value = 0.51
      refreshed = {p.key: p.value for p in ext.state_control_ext(sm).params}
      self.assertEqual(refreshed['disable_BP_lat_UI'], b'1')
      self.assertEqual(refreshed['send_hands_free_cluster_msg'], b'1')
      self.assertEqual(len(params.calls), 38)

  def test_live_inputs_and_engagement_fields_still_update_between_refreshes(self):
    params = _CountingParams(Params())
    ext = self.ext(params)
    sm = _SM()
    sm.seen.update(modelV2=True, vehicleParameters=True)
    sm['modelV2'].laneLines = [{'x': [0.] * 33, 'y': [0.] * 33} for _ in range(3)]
    sm['modelV2'].laneLineProbs = [1.] * 3
    sm['modelV2'].laneLineStds = [0.] * 3
    sm['modelV2'].position.x = [0.] * 33
    sm['modelV2'].position.y = [0.] * 33
    sm['modelV2'].orientationRate.z = [0.] * 33
    with mock.patch('openpilot.sunnypilot.selfdrive.controls.controlsd_ext.time.monotonic', return_value=0.0) as clock:
      before = ext.state_control_ext(sm)
      sm['modelV2'].position.x = [float(i) for i in range(33)]
      sm['vehicleParameters'].roll = 0.1
      sm['radarState'].leadOne.present = True
      sm['radarState'].leadOne.vLead = 12.5
      sm['selfdriveStateSP'].mads.enabled = True
      sm['selfdriveStateSP'].mads.active = True
      sm['selfdriveStateSP'].intelligentCruiseButtonManagement.vTarget = 22.
      clock.return_value = 0.01
      after = ext.state_control_ext(sm)
    self.assertEqual(len(params.calls), 19)
    self.assertEqual([p.to_dict() for p in before.params], [p.to_dict() for p in after.params])
    self.assertNotEqual(list(before.lateralInputs.pathX), list(after.lateralInputs.pathX))
    self.assertEqual(list(after.lateralInputs.pathX), [float(i) for i in range(33)])
    self.assertAlmostEqual(after.lateralInputs.roll, 0.1)
    self.assertTrue(after.leadOne.status)
    self.assertEqual(after.leadOne.vLead, 12.5)
    self.assertTrue(after.mads.enabled and after.mads.active)
    self.assertEqual(after.intelligentCruiseButtonManagement.vTarget, 22.)

  def test_non_lateral_host_does_not_read_or_publish_settings(self):
    params = _CountingParams(Params())
    result = self.ext(params, required=False).state_control_ext(_SM())
    self.assertEqual(len(result.params), 0)
    self.assertEqual(params.calls, [])

  def test_new_instance_does_not_inherit_old_settings(self):
    p1 = _CountingParams(Params(), disable_BP_lat_UI=False)
    p2 = _CountingParams(Params(), disable_BP_lat_UI=True)
    with mock.patch('openpilot.sunnypilot.selfdrive.controls.controlsd_ext.time.monotonic', return_value=0.0):
      first = self.ext(p1).published_params()
      second = self.ext(p2).published_params()
    self.assertEqual(next(p['value'] for p in first if p['key'] == 'disable_BP_lat_UI'), b'0')
    self.assertEqual(next(p['value'] for p in second if p['key'] == 'disable_BP_lat_UI'), b'1')
    self.assertEqual(len(p1.calls), 19)
    self.assertEqual(len(p2.calls), 19)
