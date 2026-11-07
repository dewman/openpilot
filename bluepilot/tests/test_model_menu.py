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
    def all_checks(self, names):
      return True
  state = SimpleNamespace(is_offroad=lambda: not onroad, sm=Messages(sm))
  with patch.object(menu, 'ui_state', state):
    assert menu.selection_allowed() is expected


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
