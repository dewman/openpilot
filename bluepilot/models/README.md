# Model switching and offline Favorites

The supported flow is: disengage cruise **and MADS steering**, select a model,
wait for **Model ready**, then press SET/RESUME or LKAS to engage again. There is
no speed, standstill, parking, or ignition-off requirement for an onroad switch.

`models_manager` downloads and SHA-verifies candidates without changing the
running selection. Manager then waits for fresh inactive `selfdriveState`, MADS,
`carControl`, and factory cruise state. A persistent transaction inhibits both
engagement state machines. Manager must receive the matching acknowledgement
from selfdrived and fresh inactive control messages before restarting only
`modeld`/`modeld_tinygrad` and `plannerd`.

`card`, `controlsd`, `selfdrived`, and `pandad` stay running. No CAN safety mode or
vehicle ECU is restarted. This avoids intentionally interrupting the Ford CAN
command loop; it does **not** establish that a moving-vehicle switch is free of
vehicle faults. Hardware validation remains necessary.

The correct runner and planner must remain alive and produce fresh, valid,
finite output for two seconds. Planner and controls timestamps must show they
consumed post-restart data. The switch times out after 60 seconds and attempts
the previous model once. Failed recovery leaves assistance locked out. An
interrupted manager transaction restores the previous model under the same lock.
Unlocking never engages assistance: a fresh driver button press is required.
Calibration and learned torque parameters are not reset by an onroad selection.

## Favorites without Wi-Fi

`bp_model_cache` refreshes the catalog independently and downloads Favorites
while offroad. The picker marks verified downloads **Offline ready**. Model
weights and their metadata live on device storage, survive reboots, and are
preserved by Clear Model Cache. Removing a Favorite releases its protection
from subsequent cache clearing. Favorites must finish downloading at least once
while connected; newly marked or incomplete Favorites are not offline-ready.

The foreground selector uses the locally saved catalog, including pinned
Favorites no longer present in a refreshed catalog. A fully cached model is
verified and loaded locally without a catalog request or artifact download.
Corrupt/incomplete files require downloading again. Verification streams 1 MiB
blocks; all Favorites are not retained in RAM. Downloads use temporary files,
verify hashes before publishing, and preserve the previous cache on failure.

## Validation

Isolated tests (also usable before building the native extensions):

```sh
.venv/bin/python -m pytest -c /dev/null --confcutdir=bluepilot/tests -p no:cacheprovider \
  bluepilot/tests/test_model_switch.py bluepilot/tests/test_model_cache.py
```

After building the native extensions, also run
`bluepilot/tests/test_model_switch_integration.py`, which exercises the real
stock/MADS state machines, typed Params, catalog fallback, and cached downloads.

Before using this on a truck, build the changed Params/Cereal definitions and
validate on a device/replay: both runner directions, cached/offline Favorites,
MADS-only engagement, attempts to engage during loading, download cancellation,
load failure, rollback failure, process death, and an ignition transition during
loading. Verify continuous CAN/control publication and no vehicle diagnostic
faults before conducting a controlled moving-vehicle trial. Local unit tests do
not validate ECU behavior or device memory/timing under inference load.
