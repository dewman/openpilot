# Cached model handover

The comma 4 Models menu allows selection while moving after both steering
(including paused MADS) and cruise control are disengaged. Onroad selections
are limited to compatible Favorites cached for the connected hardware. Small
and Chestnut models retain their separate selection slots.

The offroad `models_manager` process uses `ModelManagerBP`, extending the
SunnyPilot manager. It downloads all `ModelManager_Favs`, pins their manifests,
checks SHA256, and records file signatures. Transfers publish atomically;
interrupted downloads cannot truncate an existing model. Clear Cache preserves
Favorites, both active slots, and their warmup caches. Missing favorites retry
offroad, and removed catalog entries remain pinned. Older bp-exp pin/status
formats are migrated and reverified. No network call or large checksum runs in
the onroad UI or manager handover path.

The manager owns a persistent transaction and restarts only modeld and plannerd.
Before stopping either, it requires selfdrived to acknowledge the transaction
and publish fresh inactive steering/cruise/control state. Selfdrived uses its
existing carState subscription to relay factory-cruise status. Both stock and
MADS engagement are inhibited until completion, followed by a fresh manual
engagement-button press.

Every wait is bounded: 5 seconds for the interlock, SIGKILL escalation after
3 seconds of shutdown, 7 seconds maximum for shutdown, and 30 seconds per model
load. Completion requires 2 continuous seconds of fresh valid model, planner,
and control output from the new graph, including planner source timestamps and
the expected hardware. A failed candidate automatically restores the previous
bundle under the same lock. A failed restoration leaves assistance unavailable
and exposes **retry previous model**. The status uses the ordinary Models panel;
it never opens a blocking switching screen. Deadlines still advance when IPC
messages are stale. A manager restart recovers the persisted previous selection.

Small-model Favorites also participate in offroad warmup. Workers have a
30-second limit and are terminated when ignition changes or the parent exits.
Device GPU initialization and inference still need hardware verification; local
tests simulate inference and process restarts.

Validation:

```sh
PYTHONPATH=.:opendbc_repo:msgq_repo:tinygrad_repo:rednose_repo ZMQ=1 \
  .venv/bin/pytest -q -o addopts='' bluepilot/tests/test_model_*.py \
  openpilot/sunnypilot/models/tests/test_manager_download.py \
  openpilot/sunnypilot/mads/tests/test_mads_state_machine.py \
  openpilot/sunnypilot/mads/tests/test_mads_steering_mode.py
```

The messaging regression forces native msgq in isolated subprocesses and fills
all 25 carState reader slots. Native Params and IPC libraries must be built for
these checks. GPU hangs, actual model startup latency, and repeated switches on
the comma remain device checks before relying on this feature in the vehicle.
