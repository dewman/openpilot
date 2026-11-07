from pathlib import Path
from types import SimpleNamespace

from openpilot.bluepilot.validate_onroad import audit_settings, setting_constraints


def test_audit_reports_invalid_settings_without_modifying_files(tmp_path):
  values = {'Enabled': (b'yes', 'BOOL'), 'Gain': (b'NaN', 'FLOAT'), 'Mode': (b'3', 'INT'),
            'Offset': (b'0.6', 'FLOAT'), 'Valid': (b'1', 'BOOL'), 'Private': (b'secret', 'STRING')}
  for key, (value, _) in values.items():
    (tmp_path / key).write_bytes(value)
  params = SimpleNamespace(all_keys=lambda: list(values), get_param_path=lambda k: str(tmp_path / k),
                           get_type=lambda k: SimpleNamespace(name=values[k][1]))
  report = audit_settings(params, {'Mode': {'options': 2}, 'Offset': {'min': -0.5, 'max': 0.5}, 'Valid': {'options': 2}})
  assert {issue['key'] for issue in report['issues']} == {'Enabled', 'Gain', 'Mode', 'Offset'}
  assert 'secret' not in str(report)
  assert all((tmp_path / key).read_bytes() == value for key, (value, _) in values.items())


def test_ui_and_lateral_constraints_are_discovered():
  constraints = setting_constraints(Path(__file__).resolve().parents[2])
  assert constraints['FordPrefLateralControl']['min'] == 0
  assert constraints['FordPrefLateralControl']['max'] == 1
  assert constraints['FordHighSpeedDampening_ang']['min'] == 0.25
  assert constraints['LagdToggleDelay']['min'] == 0.05
  assert constraints['LagdToggleDelay']['max'] == 0.5
