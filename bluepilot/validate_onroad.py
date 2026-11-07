"""Offline preflight and read-only settings audit. Run with the project's Python.

  .venv/bin/python bluepilot/validate_onroad.py
  PYTHONPATH=/data/openpilot /usr/local/venv/bin/python bluepilot/validate_onroad.py --audit-settings

This never engages a vehicle, publishes CAN, resets settings, or uses a GPU.
"""
import argparse
import ast
import datetime
import json
import math
import os
from pathlib import Path
import subprocess
import sys


def setting_constraints(root):
  """Read literal UI limits and option lists without constructing UI widgets."""
  constraints = {}
  for path in (root / 'openpilot/selfdrive/ui').rglob('*.py'):
    if 'settings' not in path.parts:
      continue
    for node in ast.walk(ast.parse(path.read_text())):
      if not isinstance(node, ast.Call) or len(node.args) < 2:
        continue
      name = node.func.id if isinstance(node.func, ast.Name) else ''
      key = node.args[1]
      if 'Param' not in name or not isinstance(key, ast.Constant) or not isinstance(key.value, str):
        continue
      bounds = {}
      for kw in node.keywords:
        if kw.arg in ('min', 'max'):
          try:
            bounds[kw.arg] = ast.literal_eval(kw.value)
          except (ValueError, TypeError):
            pass
      if bounds:
        constraints.setdefault(key.value, {}).update(bounds)
      if 'Multi' in name and len(node.args) > 2 and isinstance(node.args[2], (ast.List, ast.Tuple)):
        constraints.setdefault(key.value, {})['options'] = len(node.args[2].elts)
  # The lateral library's manifest is authoritative for its settings.
  from opendbc.bluepilot_lateral.params.manifest import SETTINGS
  for setting in SETTINGS:
    if setting.range:
      constraints.setdefault(setting.key, {}).update(min=setting.range[0], max=setting.range[1])
  return constraints


def audit_settings(params, constraints):
  """Inspect registered stored values; report keys/reasons, never private values."""
  issues, checked, bounded, options = [], 0, 0, 0
  for raw_key in params.all_keys():
    key = raw_key.decode() if isinstance(raw_key, bytes) else raw_key
    path = Path(params.get_param_path(key))
    if not path.is_file():
      continue
    raw = path.read_bytes()
    if not raw:  # Params treats empty files as unset.
      continue
    checked += 1
    kind = params.get_type(key).name
    try:
      if kind == 'BOOL':
        if raw not in (b'0', b'1'):
          raise ValueError('invalid boolean')
        value = raw == b'1'
      elif kind == 'INT':
        value = int(raw)
      elif kind == 'FLOAT':
        value = float(raw)
        if not math.isfinite(value):
          raise ValueError('nonfinite number')
      elif kind == 'JSON':
        value = json.loads(raw)
      elif kind == 'TIME':
        value = datetime.datetime.fromisoformat(raw.decode())
      elif kind == 'STRING':
        value = raw.decode()
      else:
        continue
    except (ValueError, UnicodeError, OverflowError):
      issues.append({'key': key, 'issue': f'invalid {kind.lower()} encoding'})
      continue
    limits = constraints.get(key, {})
    if isinstance(value, (int, float)) and ('min' in limits or 'max' in limits):
      bounded += 1
      if value < limits.get('min', -math.inf) or value > limits.get('max', math.inf):
        issues.append({'key': key, 'issue': 'outside declared range'})
    if 'options' in limits:
      options += 1
      valid_index = type(value) is int or (type(value) is bool and limits['options'] == 2)
      if not valid_index or not 0 <= value < limits['options']:
        issues.append({'key': key, 'issue': 'invalid option index'})
  return {'stored_keys_checked': checked, 'range_constraints_checked': bounded,
          'option_constraints_checked': options, 'issues': issues}


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument('--audit-settings', action='store_true')
  args = parser.parse_args()
  root = Path(__file__).resolve().parents[1]
  paths = [str(root / p) for p in ('', 'opendbc_repo', 'msgq_repo', 'tinygrad_repo', 'rednose_repo')]
  sys.path[:0] = paths
  if args.audit_settings:
    from openpilot.common.params import Params
    report = audit_settings(Params(), setting_constraints(root))
    print(json.dumps(report, indent=2))
    return bool(report['issues'])
  env = dict(os.environ, PYTHONPATH=os.pathsep.join(paths), ZMQ='1', TINYGRAD_TESTS='1', DEV='CPU', DEBUG='0')
  suites = [
    'bluepilot/tests',
    'openpilot/selfdrive/car/tests/test_car_interfaces.py',
    'openpilot/sunnypilot/mads/tests',
    'openpilot/sunnypilot/modeld_v2/tests/test_plain_artifact.py',
    'openpilot/sunnypilot/models/tests/test_manager_download.py',
    'opendbc_repo/opendbc/sunnypilot/car/ford/tests',
  ]
  # Run BP regressions plus Ford interface startup/update/apply under randomized commands.
  return subprocess.call([sys.executable, '-m', 'pytest', '-q', '-o', 'addopts=',
                          *suites, '-k', 'not TestCarInterfaces or FORD'], cwd=root, env=env)


if __name__ == '__main__':
  raise SystemExit(main())
