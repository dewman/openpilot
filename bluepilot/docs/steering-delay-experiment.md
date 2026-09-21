# Steering delay selection and Ford angle diagnostics

**Capture ended September 21, 2026:** continuous publication of the temporary
`angleDiagnostics` snapshot and model timing fields has been removed. The fixed
delay selection fix remains active. The schemas remain readable for the existing
captures; the diagnostic descriptions below document those captures, not current
live telemetry. Controller calculations and vehicle settings are unchanged.

Branch `dmitry-bp-exp` fixes the manual steering-delay selection and adds the
measurements needed to investigate the Raptor's straight-road oscillation.
It does not establish that oscillation is fixed or introduce a new steering tune.

## Delay selection

Previously, disabling **Live Learning Steer Delay** made modeld ignore the fixed
value in `LagdValueCache` and continue using `liveDelay.lateralDelay`. The Ford
angle strategy also read the live publication directly, regardless of the toggle.

The shared resolver now has explicit modes:

- **Live:** use `liveDelay.lateralDelay`. That publication already includes lagd's
  validity/fallback logic; `live` does not necessarily mean calibration is complete.
- **Fixed:** nominal `CarParams.steerActuatorDelay` plus `LagdToggleDelay` (software
  seconds). Validate software delay against the existing UI range, 0.05–0.50 s;
  unreadable/nonfinite values use the existing 0.20 s default.
- **Startup fallback:** if live timing is zero, negative or nonfinite, use nominal
  actuator delay plus 0.20 s until a usable live publication arrives.

The model, controls, torque-learning alignment, and Ford angle prediction use the
same rules. `LagdValueCache` remains a compatibility output and is no longer a
control input. Startup cannot reuse a cached value from another vehicle or mode.
The learner continues collecting normally in fixed mode; no calibration is erased.

Settings still poll at their existing cadences (model/controls about 3 s, Ford
about 1 s). A toggle change is not an atomic cross-process switch. Model timing
is refreshed from the publication at its polling interval, without the former
additional asynchronous cache hop; the first live publication replaces startup
fallback promptly. Controls retain frame-current live delay sampling. Use logged
consumer values to identify settled intervals after a settings change.

Ford's decision and prediction caps remain 0.15 s and 0.30 s, respectively. Its
0.05 s model-step addition, speed/curvature-dependent extra lookahead, blending,
smoothing, gains, command limits and safety configuration remain as before.
Fixed delay does not imply an uncapped Ford prediction horizon.

For the Raptor's nominal 0.22 s actuator delay, a software setting of 0.12 s means
**0.34 s total**, approximately the learned value in the September 21 drive.
The UI software setting is not the total delay. This is a comparison baseline,
not a validated replacement tune. No device parameters are changed by this patch.

## Recorded diagnostics

Use full **rlogs**. `modelDataV2SP` is not included in qlogs, and qlog decimation
cannot preserve every steering update.

`modelDataV2SP` adds:

- `lateralDelay`: selected seconds before smoothing/frame offsets.
- `lateralDelaySource`: `live`, `fixed`, or `fallback`; empty in older logs.
- `lateralActionTime`: actual lookup time passed to model action extraction.
  Some model architectures output curvature directly, so this is the passed
  argument, not a claim that every architecture uses it identically.
- `modelMonoTime`: exact matching `modelV2` event timestamp.

`controllerStateBP.angleDiagnostics` records a coherent snapshot at the 20 Hz
Ford angle update, republished at the existing 100 Hz topic cadence:

| Fields | Meaning |
|---|---|
| `valid`, `controlMonoTime`, `controlFrame` | Completed active angle calculation and originating tick; deduplicate repeated publications by tick |
| `modelMonoTime`, `modelAge`, `liveDelayMonoTime` | Inputs actually consumed; model age is publication age, excluding inference/camera age |
| `delay`, `delaySource`, `decisionHorizon`, `predictionHorizon` | Selected timing and actual capped/extended lookahead |
| `desiredCurvature`, `rawPredictedCurvature`, `predictedCurvature`, `blendWeight`, `blendedCurvature` | Planner request, prediction before/after smoothing, and blend |
| `curvatureBeforeClip`, `commandedCurvature`, `measuredCurvature`, `pinionFeedback` | Trimmed request, deviation-limited request, and the feedback used |
| `curvatureGain`, `pathAngleBeforeLimits`, `pathAngleBeforeHold`, `pathAngle` | Actual gain and command through the remaining shaping stages |
| `smoothingEnabled`, `smoothingStrength` | Controller-latched configuration for this tick; effective strength is menu minus one |

Curvatures are in 1/m, path angles in radians, durations in seconds, and timestamps
in monotonic nanoseconds. `pathAngle` is the strategy output; CAN packing negates
and quantizes it. Use `sendcan` for the exact transmitted value.

Disengagement, human-turn override, stall blips and other lateral strategies do
not leave the previous command snapshot valid. Old logs decode `valid=false`.
The diagnostic data is not read by the control calculation.

## Isolation procedure

First collect a baseline with the existing live setting and unchanged gain,
smoothing, model and profile. Check whether reversals originate in planner output,
prediction/blending, command shaping, or measured motion following the command.
Correlate body yaw from both the Ford sensor and calibrated comma IMU; wheel angle
is a different response endpoint. A plausible scalar delay alone does not establish
correct small-command gain or frequency-dependent response.

For a subsequent controlled experiment, compare one timing change at a time in
closed-loop simulation before a vehicle trial. Around the observed 0.34 s total,
0.29/0.39 s are diagnostic sensitivity candidates, not recommended road settings.
Keep gains and smoothing fixed; do not remove Ford's caps in the same comparison.
Check lane-position error, the 4–5 s motion component, tracking lag, larger curves,
and override/limiter behavior. Smoother command traces alone are not a pass.
A replay with recorded vehicle motion cannot prove closed-loop improvement.

## Validation

Validation passed 99 focused tests (plus two subtests), 65 model initialization/
recovery tests, and the randomized Raptor interface test. Lint adds no findings;
seven existing findings in the Ford controller/angle strategy remain unchanged.

Focused tests cover selection/validation, mode transitions, native typed Params,
asynchronous cache compatibility, model diagnostic serialization, control timing,
Ford calculation stages, and inactive/override/blip resets. Existing smoothing,
lane-centering and model initialization/recovery tests are included. Python capnp
loads the extended schema and round-trips the new nested dataclass fields.

A differential check against baseline `4e49b36a8b` compared 10,000 learned-mode
steering updates and packed LateralMotionControl2 messages exactly, including
smoothing on/off, five nonzero live delays, turns, overrides and disengagements.
This checks command preservation for those inputs; it is not a closed-loop road
validation, and fixed-mode behavior intentionally changes. Startup fallback and
model delay-cache refresh timing also intentionally change.

Tests run in an isolated directory using the comma's native compiled dependencies,
without switching its installed checkout or changing driving parameters. A full
firmware build, full safety suite and road validation are not claimed. No safety
firmware or CAN limits are modified.

## Capture retirement

The temporary controller snapshot was being repeated on the existing 100 Hz
`controllerStateBP` topic even though steering updates run at 20 Hz. There was no
additional file logger. On two one-minute segments of route
`00000033--b3b4ab78d5`, removing the temporary controller/model fields and
recompressing at loggerd's Zstandard level 10 saved approximately 69–76 kB per
minute (about 4–5 MB/hour, 0.6% of rlog size). This is a recompression estimate,
not an exact measurement of all historical routes or qlogs.

The current publisher omits the nested snapshot entirely, rather than repeatedly
serializing an empty diagnostic struct. Both model publishers stop populating the
temporary timing fields. Tests verify absence of the nested payload, preservation
of regular controller telemetry, and continued decoding of historical snapshots.
No collected routes are deleted by this change.
