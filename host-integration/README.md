# Maintained host integration

These components are staged with desktop releases. Installation is explicit;
they do not select a GPU driver, reload kernel modules, restart GNOME, or change
the monitor layout.

`gnome/lg-edge-warp@sagecat.local` preserves the existing16ms pointer sampling
and250ms warp cooldown. It caches neighboring geometry on monitor changes;
policy tests cover hotplug/cache isolation. The previous A/B trial did not
identify this utility as the microstutter cause. Its GetState reports the loaded
build separately from staged source.

`bin/gnome-kms-recover` replaces dated diagnostic-directory scripts with a
maintained, inert-by-default recovery hook. Before a separately authorized KMS
experiment, arm the exact proposed and known-working files with
`gnome-kms-recover --arm --trial FILE --fallback FILE`. Arming does not install
the trial. On Shell failure, recovery only restores the fallback if the current
file still has the exact recorded trial hash and the fallback is unchanged.
Later user edits are preserved. No GPU/module unload/reload is performed.

The current workstation continues to use `MUTTER_DEBUG_FORCE_KMS_MODE=simple`
and `MUTTER_DEBUG_KMS_THREAD_TYPE=user`. They were introduced after a driver/KMS
login failure; a newer installed driver is not proof that these safeguards can
be removed. Version applicability and removal need a separate controlled trial.
Old diagnostics may be retained as history; production units must point to the
maintained release path, not a dated investigation directory.
