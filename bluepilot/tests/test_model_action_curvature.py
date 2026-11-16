"""Direct action scaling matches upstream; plan, blend and hold behavior remain intact."""
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from openpilot.cereal import log
from openpilot.sunnypilot.modeld_v2 import modeld


@pytest.fixture
def model():
  # Exercise the actual conversion method without loading a GPU or model weights.
  state = modeld.ModelState.__new__(modeld.ModelState)
  state.generation = 12
  state.MIN_LAT_CONTROL_SPEED = 0.3
  state.LAT_SMOOTH_SECONDS = 0.
  state.LONG_SMOOTH_SECONDS = 0.
  state.PLANPLUS_CONTROL = 1.
  state.constants = SimpleNamespace(T_IDXS=np.arange(33))
  return state


@pytest.mark.parametrize('speed,expected', [(0.5, 0.04), (1., 0.04), (2., 0.04), (2.5, 0.04), (5., 0.01), (20., 0.000625)])
def test_direct_action_curvature_at_low_and_high_speeds(model, speed, expected):
  action = model.get_action_from_model({'action': np.array([[0.25, -0.5]])}, log.ModelDataV2.Action(), 0.2, 0.2, speed)
  assert action.desiredCurvature == pytest.approx(expected)
  assert action.desiredAcceleration == pytest.approx(-0.5)
  assert not action.shouldStop


def test_stationary_curvature_hold_and_stop_are_preserved(model):
  previous = log.ModelDataV2.Action(desiredCurvature=0.007)
  action = model.get_action_from_model({'action': np.array([[0.25, -0.5]])}, previous, 0.2, 0.2, 0.2)
  assert action.desiredCurvature == pytest.approx(previous.desiredCurvature)
  assert action.shouldStop


def test_blend_gets_corrected_curvature_before_existing_smoothing(model):
  blend = Mock(return_value=0.003)
  action = model.get_action_from_model({'action': np.array([[0.25, 0.]])}, log.ModelDataV2.Action(), 0.2, 0.2, 1., blend)
  blend.assert_called_once_with(pytest.approx(0.04))
  assert action.desiredCurvature == pytest.approx(0.043)


@pytest.mark.parametrize('speed', [0.5, 1., 2.5, 20.])
def test_plan_output_path_keeps_its_own_calculation(model, monkeypatch, speed):
  curvature = Mock(return_value=0.017)
  monkeypatch.setattr(modeld, 'get_curvature_from_output', curvature)
  monkeypatch.setattr(modeld, 'get_accel_from_plan', Mock(return_value=0.25))
  output = {'plan': np.zeros((1, 33, 15))}
  action = model.get_action_from_model(output, log.ModelDataV2.Action(), 0.2, 0.2, speed)
  curvature.assert_called_once()
  assert curvature.call_args.args[2] == speed
  assert action.desiredCurvature == pytest.approx(0.017)
  assert action.desiredAcceleration == pytest.approx(0.25)
