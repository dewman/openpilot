"""Exercise the real BP entry points after the openpilot/ source-layout move."""
from pathlib import Path


def test_bluepilot_detection_follows_checkout_root(tmp_path, monkeypatch):
  from openpilot.common import bluepilot
  try:
    with monkeypatch.context() as context:
      context.setattr(bluepilot, 'BASEDIR', str(tmp_path))
      bluepilot.is_bluepilot.cache_clear()
      assert not bluepilot.is_bluepilot()
      (tmp_path / 'BPVERSION').write_text('7.0')
      bluepilot.is_bluepilot.cache_clear()
      assert bluepilot.is_bluepilot()
  finally:
    bluepilot.is_bluepilot.cache_clear()


def test_mici_bp_entry_points_are_enabled_and_importable():
  from openpilot.common.bluepilot import is_bluepilot
  from openpilot.selfdrive.ui.sunnypilot.mici.layouts.settings import ModelsLayoutMici
  from openpilot.selfdrive.ui.bp.mici.onroad.augmented_road_view_bp import MiciAugmentedRoadViewBP
  from openpilot.system.manager.process_config import managed_processes
  assert is_bluepilot()
  assert ModelsLayoutMici.__name__ == 'ModelsLayoutMiciBP'
  assert MiciAugmentedRoadViewBP.__name__ == 'MiciAugmentedRoadViewBP'
  assert 'bp_portal' in managed_processes
  from openpilot.common.basedir import BASEDIR
  assert Path(BASEDIR, 'BPVERSION').is_file()
