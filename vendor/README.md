# Offline SDK inputs

The deployment bundle is generated on a machine that has network access. The
target robot does not need network access.

The private SDK wheels are deliberately **not stored in Git**. Keep them in a
separate local directory or artifact store and pass their paths to `bundle.sh`.
The TacCap-Gripper wheel is built from the official repository:

`https://github.com/XenseRobotics-AI/TacCap-Gripper.git`

Run `bundle.sh --xensesdk-wheel PATH --taccap-wheel PATH` to collect a
compatible Python runtime, both private SDK wheels and all public Python
wheels into the ignored `offline/` directory before deploying to an isolated
target.
