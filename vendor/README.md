# Offline SDK inputs

The deployment bundle is generated on a machine that has network access. The
target robot does not need network access.

`vendor/wheels/` contains the TacCap-Gripper wheel built from the official
repository:

`https://github.com/XenseRobotics-AI/TacCap-Gripper.git`

Run `bundle.sh` to collect a compatible Python runtime, `xensesdk`,
TacCap-Gripper and all public Python wheels into the ignored `offline/`
directory before deploying to an isolated target.
