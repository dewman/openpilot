# 2023 F-150 Raptor V6 vehicle profile

`FORD_F_150_RAPTOR_MK3` is an explicitly selected vehicle model for the 2023 V6
Raptor. It corrects the generic F-150 dimensions without changing other F-150s.
It is not a new neural driving model or a fitted suspension/roll controller, and
does not establish that the freeway oscillation is fixed.

## Nominal model inputs

| Input | Raptor | Previous generic F-150 |
|---|---:|---:|
| Wheelbase | 3.694 m | 3.990 m |
| Base curb mass | 2611 kg | 3334 kg |
| Mass including standard 136 kg payload | 2747 kg | 3470 kg |
| Nominal steering ratio | 17.2 | 17.0 |

Source: Ford's **2023 F-150 eSourceBook**, 9 September 2022, printed pages 4,
18 and 29: 145.4 inches, 5757 lb base curb mass for standard Raptor, 17.2:1.
[Ford-authored specification PDF](https://madocumentupload.marketingassociates.com/api/Document/GetFile?v1=7382090&v2=090922110821&v3=60&v4=e67a24a2227fe34c54c9c216482b94cb1fee5b9b0af9fed30f32c57d&v5=False).
Mass is rounded to kg and is a base specification, not a scale measurement of
this modified truck. Options, wheel/tire changes and actual payload are unknown.
The standard 136 kg payload already allows for occupants/cargo; do not add the
driver again on top. A usual-load scale weight should replace the modeled total
by setting base mass to that total minus the standard payload. A factory payload
sticker can help infer the as-built curb mass (GVWR minus rated payload), but
does not capture subsequent tire/equipment changes.

The existing Ford center-to-front assumption (44% of wheelbase), tire stiffness
factor and inertia scaling remain in use. These are model approximations, not
measured Raptor tire stiffness, center of gravity or yaw inertia. The normal
interface recalculates stiffness and inertia from the new dimensions and mass.
The separate pinion geometry row 13 matches the resulting nominal model; all
existing firmware command limits remain unchanged. Both pinion toggle states
remain supported. F-150 bypass anti-overshoot behavior, BP platform gains, menu
settings and nominal actuator delay are retained.

## Aftermarket 37-inch KO2 tires and camera height

The owner reports factory-spec BFG KO2 37-inch tires fitted to a truck that did
not have the factory 37 Performance Package. Do not apply that package's curb
mass, preload or suspension-travel specification based on tire size alone.

Nominal 35-to-37-inch diameter change means about one inch of unloaded radius
change, not two inches of ride height. Loaded radius, pressure, suspension
settling and lens mounting position prevent that from being an absolute camera
height measurement. No fixed height or extra tire-derived increment is added.

Keep `CalibrationParams`: calibrationd continues estimating height and camera
orientation. SunnyPilot modeld_v2's `CameraOffsetHelper` uses live calibrated
height for camera-offset shear. Its regression test covers a 1.62 m estimated
height and verifies that no extra tire offset is added. The ordinary model warp
uses orientation/intrinsics; this profile does not pretend to retrain vision or
introduce height normalization into the neural network. A physical lens-height
measurement would help assess estimator bias but is not available yet.

## Selection and calibration migration

Select **Ford F-150 Raptor 2023** in the vehicle selector while offroad. This
writes the normal `CarPlatformBundle`; it does not rewrite firmware or VIN
fingerprints shared with generic F-150s. Changing the fingerprint identity lets
paramsd and lagd reject saved generic-F-150 estimates on the next drive. Tests
cover rejection, same-profile reuse and preservation of camera calibration.

For the initial installation, back up the existing vehicle selection and saved
learner parameters before selecting Raptor. Clear the old `FordAngleAutoCalState`
evidence after backup; preserve the enable/lock switches and current adjustment
factors. Vehicle delay/steering estimates will relearn, so
the initial segment is not a settled comparison. Rollback is selection of the
previous F-150 profile and, if desired, restoration of its backed-up parameters
while offroad.

`controllerStateBP` now logs controller-latched smoothing enable/effective
strength, low-curve factor and pinion-source selection. Combine these with
`carParams`, `liveParameters`, `liveDelay` and `liveCalibration` on the next route.
Only a subsequent controlled drive can establish whether sway improves.

## Validation scope

The focused model, camera-height, learner-migration and controller telemetry
tests pass, including compiled C safety geometry checks and a randomized full
Raptor interface/controller test. A baseline comparison of the broader Ford
firmware tests found 457 passing tests unchanged; the new profile adds 117
passing cases. The baseline also has 50 failing legacy expectations, with 10
equivalent inherited failures in the new Raptor classes. These include old
four-signal steering, curvature-limit and forwarding expectations. The complete
Ford suite is therefore not green. No existing passing case regressed and the
Raptor inherited failures match the corresponding baseline failures. This
change does not alter command permissions or limits to satisfy those tests.
