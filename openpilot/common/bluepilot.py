"""BluePilot detection. Safe to import on any fork — returns False when not BluePilot."""
import os
from functools import cache
from openpilot.common.basedir import BASEDIR

@cache
def is_bluepilot() -> bool:
  return os.path.isfile(os.path.join(BASEDIR, 'BPVERSION'))
