"""Render BP buttons through the real upstream button API without a GPU window."""
from types import SimpleNamespace
from unittest.mock import Mock

import pyray as rl
import pytest

from openpilot.selfdrive.ui.mici.widgets import button as upstream
from openpilot.selfdrive.ui.bp.mici.widgets import button_bp as bp


@pytest.mark.parametrize('icon_width', [0, 80])
@pytest.mark.parametrize('value', ['', 'Default'])
def test_bp_button_renders_title_and_subtitle_with_current_upstream_api(monkeypatch, icon_width, value):
  labels = []

  def label_factory(text, font_size, **kwargs):
    label = Mock()
    label.font_size = font_size
    label.set_font_size.side_effect = lambda size: setattr(label, 'font_size', size)
    label.get_content_height.side_effect = lambda width: label.font_size
    labels.append(label)
    return label

  monkeypatch.setattr(upstream, 'UnifiedLabel', label_factory)
  monkeypatch.setattr(bp.gui_app, 'texture', lambda *args, **kwargs: SimpleNamespace(width=402, height=180))
  monkeypatch.setattr(rl, 'draw_texture_ex', Mock())
  monkeypatch.setattr(rl, 'draw_texture_pro', Mock())
  icon = SimpleNamespace(width=icon_width, height=80) if icon_width else None
  button = bp.BigButtonBP('BluePilot settings', value=value, icon=icon)

  # This is the complete BP render path, including the upstream content renderer.
  # Previously the iconless case called the removed BigButton._width_hint and
  # crashed the UI when opening settings.
  button._render(button.rect)
  title_rect = labels[0].render.call_args.args[0]
  expected_width = 402 - 2 * button.LABEL_HORIZONTAL_PADDING - (icon_width + 20 if icon_width else 0)
  assert title_rect.width == expected_width
  if value:
    labels[1].get_content_height.assert_any_call(button._subtitle_width_hint())
    subtitle_rect = labels[1].render.call_args.args[0]
    assert subtitle_rect.width == 402 - 2 * button.LABEL_HORIZONTAL_PADDING
  else:
    labels[1].render.assert_not_called()
