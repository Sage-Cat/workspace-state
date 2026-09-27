#!/usr/bin/python3 -I
"""Read-only systemd verification using staged copies of installed units."""

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


SOURCE = Path(__file__).resolve().parents[1] / "system-integration"


def verify(scope):
    command = ["/usr/bin/systemctl"] + (["--user"] if scope == "user" else [])
    with tempfile.TemporaryDirectory(prefix="wsctl-shutdown-verify-") as directory:
        stage = Path(directory)
        units = []
        for entry in sorted((SOURCE / scope).iterdir()):
            if entry.is_dir() and entry.name.endswith(".d"):
                unit = entry.name[:-2]
                fragment = subprocess.check_output(command + ["show", unit, "-p", "FragmentPath", "--value"], text=True).strip()
                if not fragment:
                    raise RuntimeError(f"Required installed unit is missing: {unit}")
                shutil.copy2(fragment, stage / unit)
                destination = stage / entry.name
                destination.mkdir()
                drop_ins = subprocess.check_output(command + ["show", unit, "-p", "DropInPaths", "--value"], text=True).split()
                for drop_in in drop_ins:
                    shutil.copy2(drop_in, destination / Path(drop_in).name)
                for drop_in in entry.iterdir():
                    text = drop_in.read_text()
                    # Resolve the future root-installed helper to this exact
                    # checked-in source for verification before installation.
                    text = text.replace("/usr/local/libexec/wsctl-livepatch-stop-check", str(SOURCE / "wsctl-livepatch-stop-check"))
                    (destination / drop_in.name).write_text(text)
            else:
                unit = entry.name
                shutil.copy2(entry, stage / unit)
            units.append(str(stage / unit))
        env = dict(os.environ, SYSTEMD_UNIT_PATH=f"{stage}:")
        args = ["/usr/bin/systemd-analyze"] + (["--user"] if scope == "user" else [])
        result = subprocess.run(args + ["--generators=no", "--man=no", "verify"] + units, env=env, check=False)
        if result.returncode:
            return result.returncode
    print(f"Verified {scope} units and dependency ordering using temporary copies; no live changes.")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scope", choices=("user", "system"))
    raise SystemExit(verify(parser.parse_args().scope))
