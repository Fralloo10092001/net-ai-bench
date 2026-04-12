from pathlib import Path

from .linux_driver import LinuxDriver
from .srlinux_driver import SRLinuxDriver


def get_driver(kind: str, lab_name: str, log_file: Path):
    if kind == "linux":
        return LinuxDriver(lab_name=lab_name, log_file=log_file)

    if kind == "nokia_srlinux":
        return SRLinuxDriver(lab_name=lab_name, log_file=log_file)

    return LinuxDriver(lab_name=lab_name, log_file=log_file)