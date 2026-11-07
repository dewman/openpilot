"""Menu guards, including engagement changes while the picker is already open."""
from types import SimpleNamespace
from unittest.mock import Mock, patch
import time

import pytest
from openpilot.selfdrive.ui.bp.mici.layouts.settings import models_bp as menu


@pytest.mark.parametrize('onroad,engaged,cruise,stale,expected', [
  (False, False, False, False, True),
  (True, False, False, False, True),
  (True, True, False, False, False),
  (True, False, True, False, False),
  (True, False, False, True, False),
])
def test_menu_requires_fresh_disengaged_state(onroad, engaged, cruise, stale, expected):
  now = time.monotonic()
  sm = {
    'selfdriveState': SimpleNamespace(enabled=engaged),
    'selfdriveStateSP': SimpleNamespace(mads=SimpleNamespace(enabled=engaged)),
    'carState': SimpleNamespace(cruiseState=SimpleNamespace(enabled=cruise)),
  }
  class Messages(dict):
    logMonoTime = dict.fromkeys(sm, int((now - (2 if stale else 0)) * 1e9))
    def all_alive(self, names):
      return True
    def all_valid(self, names):
      return True
  state = SimpleNamespace(is_offroad=lambda: not onroad, sm=Messages(sm))
  with patch.object(menu, 'ui_state', state):
    assert menu.selection_allowed() is expected


def test_real_slow_ui_subscriber_does_not_block_fresh_disengaged_selection():
  from openpilot.cereal import messaging
  from openpilot.common.prefix import OpenpilotPrefix
  names = ['selfdriveState', 'selfdriveStateSP', 'carState']
  with OpenpilotPrefix():
    sm = messaging.SubMaster(names)
    now = time.monotonic()
    # The UI reads conflated snapshots at 30 Hz from nominally 100 Hz services.
    for i in range(300):
      stamp = now - 10 + i / 30
      messages = [messaging.new_message(name, valid=True, logMonoTime=int(stamp * 1e9)).as_reader() for name in names]
      sm.update_msgs(stamp, messages)
    assert not sm.all_checks(names)  # Reproduces the previous greyed-out control.
    assert sm.all_alive(names) and sm.all_valid(names)
    with patch.object(menu, 'ui_state', SimpleNamespace(is_offroad=lambda: False, sm=sm)):
      assert menu.selection_allowed()
      sm.data['selfdriveStateSP'] = messaging.new_message('selfdriveStateSP').selfdriveStateSP
      sm.data['selfdriveStateSP'].mads.enabled = True
      assert not menu.selection_allowed()
      assert 'MADS' in menu.selection_block_reason()


def test_picker_click_rechecks_engagement_before_staging():
  panel = menu.ModelsLayoutMiciBP.__new__(menu.ModelsLayoutMiciBP)
  with patch.object(menu, 'selection_allowed', return_value=False), patch.object(menu, 'stage_bundle') as stage:
    panel._select_model(Mock())
  stage.assert_not_called()


def test_offroad_default_picker_cannot_change_selection_after_ignition_transition():
  panel = menu.ModelsLayoutMiciBP.__new__(menu.ModelsLayoutMiciBP)
  state = SimpleNamespace(is_offroad=lambda: False, params=Mock())
  with patch.object(menu, 'ui_state', state):
    panel._select_default('qcom')
  state.params.remove.assert_not_called()
