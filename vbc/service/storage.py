"""Where the API may point the server: storage roots and media mounts.

Off loopback, input folders (and their _out/_err siblings) must lie inside a
storage root. Roots are the configured ``storage.roots`` plus every media
mount of the process, read from /proc/self/mountinfo on each use, so a folder
on a newly mounted disk is accepted without a restart or a deployment input.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

MOUNTINFO = Path("/proc/self/mountinfo")

# Kernel, runtime and container plumbing; never media.
_PSEUDO_FS = {
    "autofs", "binfmt_misc", "bpf", "cgroup", "cgroup2", "configfs", "debugfs", "devpts",
    "devtmpfs", "efivarfs", "fusectl", "fuse.lxcfs", "hugetlbfs", "mqueue", "nsfs", "proc",
    "pstore", "ramfs", "rpc_pipefs", "securityfs", "squashfs", "sysfs", "tracefs",
}
_SYSTEM_PREFIXES = (
    "/proc", "/sys", "/dev", "/run", "/etc", "/boot", "/usr", "/bin", "/sbin", "/lib", "/lib64",
    "/var", "/root", "/opt",
)
# File systems where inotify does not see changes made by other hosts.
_NETWORK_FS = ("nfs", "nfs4", "cifs", "smb3", "9p", "virtiofs", "ceph", "glusterfs")


def _unescape(field: str) -> str:
    """mountinfo escapes space, tab, newline and backslash as \\ooo."""
    out, i = [], 0
    while i < len(field):
        if field[i] == "\\" and i + 4 <= len(field) and field[i + 1:i + 4].isdigit():
            out.append(chr(int(field[i + 1:i + 4], 8)))
            i += 4
        else:
            out.append(field[i])
            i += 1
    return "".join(out)


def read_mounts(mountinfo: Path = MOUNTINFO) -> List[Tuple[Path, str]]:
    """(mount point, fs type) of every mount, in mount order."""
    try:
        lines = mountinfo.read_text().splitlines()
    except OSError:
        return []
    mounts = []
    for line in lines:
        left, sep, right = line.partition(" - ")
        fields = left.split()
        if not sep or len(fields) < 5 or not right.split():
            continue
        mounts.append((Path(_unescape(fields[4])), right.split()[0]))
    return mounts


def _under(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def media_mounts(exclude: Iterable[Path] = (), mountinfo: Path = MOUNTINFO) -> List[Path]:
    """Mounted directories that can hold media.

    Leaves out the root file system, kernel/runtime mounts, file bind mounts
    (/etc/hosts, injected driver libraries) and the mounts holding ``exclude``
    (the persistent volume with the config). Mounts below an excluded one stay.
    """
    mounts = read_mounts(mountinfo)
    holders = set()
    for path in exclude:
        candidates = [point for point, _fs in mounts if _under(Path(path), point)]
        if candidates:
            holders.add(max(candidates, key=lambda p: len(p.parts)))
    found: List[Path] = []
    for point, fstype in mounts:
        if point in (Path("/"), Path("/tmp")) or point in holders or fstype in _PSEUDO_FS:
            continue
        if any(_under(point, Path(prefix)) for prefix in _SYSTEM_PREFIXES):
            continue
        if not os.path.isdir(point) or point in found:
            continue
        found.append(point)
    return found


def mount_fstype(path: Path, mountinfo: Path = MOUNTINFO) -> Optional[str]:
    """File system type of the mount holding ``path``."""
    best: Optional[Tuple[Path, str]] = None
    for point, fstype in read_mounts(mountinfo):
        if _under(path, point) and (best is None or len(point.parts) >= len(best[0].parts)):
            best = (point, fstype)
    return best[1] if best else None


def is_network_fs(path: Path) -> bool:
    fstype = mount_fstype(path) or ""
    return fstype.startswith("fuse.sshfs") or fstype in _NETWORK_FS


class StorageRoots:
    """The roots in force right now (configured + detected media mounts)."""

    def __init__(self, configured: Iterable[str], *, detect_mounts: bool, exclude: Iterable[Path] = ()) -> None:
        self.configured = [Path(root) for root in configured]
        self.detect_mounts = detect_mounts
        self.exclude = [Path(path) for path in exclude]

    def current(self) -> List[Path]:
        roots = list(self.configured)
        if self.detect_mounts:
            for mount in media_mounts(self.exclude):
                if mount not in roots:
                    roots.append(mount)
        return roots
