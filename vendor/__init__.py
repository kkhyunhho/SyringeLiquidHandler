"""Vendored hardware drivers, kept in-repo so the cell runs standalone.

Packages: `sy01b` (pump), `entris_ii` (balance), `mks_motor` (XZ gantry);
plus the flat module `linear_motor_controller` (MINAS A6 linear, codename
`lmc`). Imported as `vendor.<name>`. See VENDORED.md for upstream sources,
commits, and the local changes applied — re-copy the source to update.
"""
