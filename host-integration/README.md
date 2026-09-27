# Host integration

Optional helpers included in desktop release staging:

- `gnome/lg-edge-warp@sagecat.local`: move the pointer across gaps between monitors.
- `bin/gnome-kms-recover`: restore a known-working configuration after an explicitly
  armed KMS trial fails. Later edits are left intact.

Installation is separate from staging. These helpers do not restart GNOME,
reload GPU drivers or choose a monitor layout.

## Arm a recovery trial

```sh
gnome-kms-recover --arm --trial /path/to/trial.conf --fallback /path/to/working.conf
```

Arming records file hashes; it does not install the trial. Review both files
before applying it. Existing driver workarounds should only be changed in a
controlled test.

[Deployment guide](../docs/deployment.md)
