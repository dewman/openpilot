"""Both UI actions defer their writes until a visible 3/2/1 warning completes."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import pyray as rl

from openpilot.bluepilot.tests.test_model_switch import Params
from openpilot.selfdrive.ui.bp.mici.widgets import disengage_countdown as warning


@pytest.fixture
def dialog(monkeypatch):
  clock = SimpleNamespace(now=100.)
  panel = warning.DisengageCountdownBP(Mock(), clock=lambda: clock.now)
  panel.set_enabled(True)
  panel._start()
  monkeypatch.setattr(warning.BigDialogBase, '_update_state', lambda self: None)
  monkeypatch.setattr(warning.gui_app, 'widget_in_stack', lambda widget: True)
  return panel, clock


def test_countdown_defers_action_until_all_three_seconds_and_dismissal(dialog):
  panel, clock = dialog
  for elapsed, number in [(0, 3), (.99, 3), (1, 2), (1.99, 2), (2, 1), (2.99, 1)]:
    clock.now = 100 + elapsed
    panel._update_state()
    assert panel._remaining == number
    assert not panel.is_dismissing
    panel._action.assert_not_called()
  clock.now = 103
  panel._update_state()
  assert panel.is_dismissing
  panel._action.assert_not_called()
  # NavWidget invokes this only after the warning has dismissed.
  panel._dismiss_callback()
  panel._action.assert_called_once()


def test_swiping_to_cancel_does_not_schedule_an_action(dialog):
  panel, clock = dialog
  panel._update_state()
  panel.dismiss()  # same no-callback dismissal as user navigation
  clock.now += 10
  panel._update_state()
  assert panel._dismiss_callback is None
  panel._action.assert_not_called()


def test_ui_stall_does_not_skip_countdown_digits(dialog):
  panel, clock = dialog
  panel._update_state()
  clock.now += 10
  panel._update_state()
  assert panel._remaining == 2
  assert not panel.is_dismissing


def test_covered_countdown_restarts_before_action(dialog):
  panel, clock = dialog
  panel._update_state()
  panel.set_enabled(False)
  clock.now += 10
  panel._update_state()
  panel.set_enabled(True)
  panel._update_state()
  assert panel._remaining == 3
  assert not panel.is_dismissing


def test_warning_renders_title_number_and_cancel_hint(dialog, monkeypatch):
  panel, _ = dialog
  label = Mock()
  monkeypatch.setattr(warning, 'gui_label', label)
  panel._render(rl.Rectangle(0, 0, 536, 240))
  assert [call.args[1] for call in label.call_args_list] == [
    'comma about to disengage', '3', 'take control • swipe down to cancel',
  ]


def test_assistance_toggle_defers_parameter_and_callback(monkeypatch):
  params = Params()
  params.put_bool('OpenpilotEnabledToggle', True)
  monkeypatch.setattr(warning, 'ui_state', SimpleNamespace(params=params))
  pending = Mock()
  monkeypatch.setattr(warning, 'warn_before_disengage', pending)
  toggle = warning.AssistanceToggleBP.__new__(warning.AssistanceToggleBP)
  toggle._param = 'OpenpilotEnabledToggle'
  toggle._apply_callback = Mock()
  toggle._request_change(False)
  assert params.get_bool('OpenpilotEnabledToggle')
  assert toggle._checked
  toggle._apply_callback.assert_not_called()
  pending.call_args.args[0]()
  assert not params.get_bool('OpenpilotEnabledToggle')
  toggle._apply_callback.assert_called_once_with(False)


def test_offroad_warning_executes_without_countdown(monkeypatch):
  action = Mock()
  monkeypatch.setattr(warning, 'ui_state', SimpleNamespace(is_offroad=lambda: True))
  warning.warn_before_disengage(action)
  action.assert_called_once()
