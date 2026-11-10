"""Render the real MICI BP onroad view from a local recording, with no CAN output.

The input adapter feeds recorded Cereal events to the actual UI state handler.
Engagement transitions are explicit so a disengaged-only run cannot pass.
All parameters live in an isolated OpenpilotPrefix, never the user's device store.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time


def run(args):
  import pyray as rl
  from openpilot.common.prefix import OpenpilotPrefix
  from openpilot.common.params import Params
  from openpilot.tools.lib.logreader import LogReader

  root = Path(__file__).resolve().parents[1]
  with OpenpilotPrefix():
    params = Params()
    if args.preferences:
      known = {k.decode() for k in params.all_keys()}
      for key, value in json.loads(Path(args.preferences).read_text()).items():
        if key in known:
          params.put(key, value, block=True)
    for override in args.set:
      key, value = override.split('=', 1)
      params.put(key, json.loads(value), block=True)
    from openpilot.system.ui.lib import application
    if args.assets:
      application.ASSETS_DIR = Path(args.assets).resolve()
      application.FONT_DIR = application.ASSETS_DIR / 'fonts'
      from openpilot.selfdrive.ui.bp.lib import theme_pack
      theme_pack.BUNDLED_DIR = str(application.ASSETS_DIR / 'bp_themes')
    from openpilot.selfdrive.ui.ui_state import ui_state, UIStatus
    from openpilot.selfdrive.ui.bp.mici.onroad import model_renderer_bp
    if args.baseline_ref:
      path = 'openpilot/selfdrive/ui/bp/mici/onroad/model_renderer_bp.py'
      source = subprocess.check_output(['git', 'show', f'{args.baseline_ref}:{path}'], cwd=root, text=True)
      namespace = {'__name__': 'bp_ui_replay_baseline'}
      exec(compile(source, path, 'exec'), namespace)
      model_renderer_bp.ModelRendererBP = namespace['ModelRendererBP']
    from openpilot.selfdrive.ui.bp.mici.onroad.augmented_road_view_bp import MiciAugmentedRoadViewBP

    latest, recorded = {}, []
    for event in LogReader(args.log):
      if event.which() in ui_state.sm.services:
        builder = event.as_builder()
        recorded.append((int(event.logMonoTime), builder))
        latest.setdefault(event.which(), builder)
    recorded.sort(key=lambda item: item[0])
    source_start, source_end = recorded[0][0], recorded[-1][0]
    source_index, source_events = 0, 0
    required = ('modelV2', 'extrinsicsCalibration', 'carState', 'carControl', 'selfdriveState',
                'selfdriveStateSP', 'deviceState', 'carParams', 'pandaStates')
    missing = set(required) - latest.keys()
    if missing:
      raise RuntimeError(f'Recording lacks required inputs: {sorted(missing)}')
    cp_bytes = latest['carParams'].carParams.as_reader().as_builder().to_bytes()
    for key in ('CarParams', 'CarParamsPersistent'):
      params.put(key, cp_bytes, block=True)
    ui_state.update_params()
    # Cloud access is outside this rendering test; the real UI message/state/render paths run below.
    ui_state.prime_state.start = lambda: None
    # Refresh preferences synchronously; do not leave an infinite worker using
    # the isolated store after OpenpilotPrefix removes it at shutdown.
    ui_state._params_thread = threading.current_thread()
    latest['deviceState'].deviceState.started = True
    latest['deviceState'].deviceState.deviceType = 'mici'
    latest['pandaStates'].pandaStates[0].ignitionLine = True
    latest['selfdriveState'].selfdriveState.alertSize = 'none'
    latest['selfdriveState'].selfdriveState.alertType = ''

    frame = 0
    mode = 'disengaged'
    modes = ('disengaged', 'lateral_only', 'engaged', 'override', 'disengaged')
    counters = dict.fromkeys(modes, 0)
    path_calls = 0
    original_path = model_renderer_bp.ModelRendererBP._draw_path

    def counted_path(self, *a, **kw):
      nonlocal path_calls
      path_calls += 1
      return original_path(self, *a, **kw)

    model_renderer_bp.ModelRendererBP._draw_path = counted_path

    def feed_recording(_timeout=0):
      nonlocal source_index, source_events
      now = time.monotonic()
      source_time = source_start + int(frame / 60 * 1e9)
      while source_index < len(recorded) and recorded[source_index][0] <= source_time:
        _, event = recorded[source_index]
        latest[event.which()] = event
        source_index += 1
        source_events += 1
      latest['deviceState'].deviceState.started = True
      latest['deviceState'].deviceState.deviceType = 'mici'
      latest['pandaStates'].pandaStates[0].ignitionLine = True
      ss = latest['selfdriveState'].selfdriveState
      ss.alertSize = 'none'
      ss.alertType = ''
      mads = latest['selfdriveStateSP'].selfdriveStateSP.mads
      control = latest['carControl'].carControl
      active = mode != 'disengaged'
      ss.enabled = mode in ('engaged', 'override')
      ss.active = ss.enabled
      ss.state = 'overriding' if mode == 'override' else 'enabled' if ss.enabled else 'disabled'
      mads.available = True
      mads.enabled = mads.active = active
      mads.state = 'overriding' if mode == 'override' else 'enabled' if active else 'disabled'
      control.enabled = ss.enabled
      control.latActive = active
      latest['carState'].carState.cruiseState.enabled = ss.enabled
      if args.leads:
        for index, lead in enumerate((latest['radarState'].radarState.leadOne, latest['radarState'].radarState.leadTwo)):
          lead.present = True
          lead.dRel = 25.0 + index * 25.0
          lead.yRel = 0.0
          lead.vRel = -1.0
      for event in latest.values():
        event.logMonoTime = int(now * 1e9)
        event.valid = True
      ui_state.sm.update_msgs(now, [e.as_reader() for e in latest.values()])

    ui_state.sm.update = feed_recording
    rl.set_config_flags(rl.ConfigFlags.FLAG_WINDOW_HIDDEN)
    gui_app = application.gui_app
    gui_app.init_window('BluePilot engaged-state replay')
    view = None if args.models_menu else MiciAugmentedRoadViewBP()
    panel = None
    if args.models_menu:
      from openpilot.selfdrive.ui.bp.mici.layouts.settings.models_bp import ModelsLayoutMiciBP
      panel = ModelsLayoutMiciBP()
    gui_app.push_widget(panel if panel is not None else view)
    rl.get_frame_time = lambda: 1 / 60
    rl.get_time = lambda: frame / 60
    screenshots = Path(args.output)
    screenshots.mkdir(parents=True, exist_ok=True)
    try:
      for _ in gui_app.render():
        mode = modes[min(frame // args.frames_per_state, len(modes) - 1)]
        ui_state.update()
        if frame % 60 == 0:
          ui_state.update_params()
        # No video decoder/vehicle camera is needed to project recorded model geometry.
        # Use the real camera calibration/projection before the next onroad render.
        if view is not None:
          view._update_calibration()
          view._calc_frame_matrix(view._content_rect)
        if panel is not None and frame % args.frames_per_state > 5:
          expected = True  # browsing/selection remains available while engaged
          if panel.select_model_btn.enabled != expected:
            raise RuntimeError(f'Model selector enable state incorrect in {mode}')
          if panel.delay_control.enabled == params.get_bool('LagdToggle'):
            raise RuntimeError('Fixed delay availability does not match learning setting')
          if frame % args.frames_per_state == 10:
            targets = [panel.select_model_btn, panel.lagd_toggle, panel.delay_control, panel.select_model_btn, panel.lagd_toggle]
            target = targets[min(frame // args.frames_per_state, len(targets) - 1)]
            panel._scroller.scroll_to(target.rect.x - panel.rect.x, smooth=False)
        if ui_state.status != UIStatus.DISENGAGED:
          counters[mode] += 1
        if frame % args.frames_per_state == args.frames_per_state - 1:
          image = rl.load_image_from_screen()
          try:
            if not rl.export_image(image, str((screenshots / f'{frame:05d}-{mode}.png').resolve())):
              raise RuntimeError('Failed to save rendered frame')
          finally:
            rl.unload_image(image)
        frame += 1
        if frame >= args.frames_per_state * len(modes):
          break
      if panel is None and path_calls < args.frames_per_state:
        raise RuntimeError(f'Engaged path was not exercised: {path_calls} calls')
      if not all(counters[k] for k in ('lateral_only', 'engaged', 'override')):
        raise RuntimeError(f'Missing engagement coverage: {counters}')
      report = {'frames': frame, 'path_draw_calls': path_calls, 'engaged_frames': counters,
                'model_points': len(view._model_renderer._path.projected_points) if view is not None else None,
                'source_events_replayed': source_events,
                'source_seconds_replayed': min(frame / 60, (source_end - source_start) / 1e9),
                'models_menu': args.models_menu,
                'recording': str(Path(args.log).resolve()), 'baseline_ref': args.baseline_ref}
      if panel is None and not report['model_points']:
        raise RuntimeError('No recorded model geometry was projected')
      (screenshots / 'report.json').write_text(json.dumps(report, indent=2))
      print(json.dumps(report))
    finally:
      gui_app.close()


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument('--log', required=True)
  parser.add_argument('--preferences')
  parser.add_argument('--assets')
  parser.add_argument('--baseline-ref')
  parser.add_argument('--set', action='append', default=[], metavar='KEY=JSON')
  parser.add_argument('--leads', action='store_true', help='Exercise radar lead-present branches')
  parser.add_argument('--models-menu', action='store_true', help='Render and check the real model menu through engagement transitions')
  parser.add_argument('--frames-per-state', type=int, default=120)
  parser.add_argument('--output', required=True)
  args = parser.parse_args()
  root = Path(__file__).resolve().parents[1]
  for name in ('', 'opendbc_repo', 'msgq_repo', 'tinygrad_repo', 'rednose_repo'):
    sys.path.insert(0, str(root / name))
  os.environ.update(ZMQ='1', DEBUG='0', DEV='CPU', TINYGRAD_TESTS='1', OFFSCREEN='1')
  run(args)


if __name__ == '__main__':
  main()
