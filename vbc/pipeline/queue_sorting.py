import random
from pathlib import Path
from typing import List

from vbc.config.models import GeneralConfig, QUEUE_SORT_CHOICES
from vbc.domain.models import VideoFile


def sort_files(
    files: List[VideoFile],
    input_dirs: List[Path],
    config: GeneralConfig,
    extensions: List[str],
) -> List[VideoFile]:
    mode = config.queue_sort

    if mode == "name":
        return sorted(files, key=lambda vf: (vf.path.name, str(vf.path)))

    if mode in ("size", "size-asc"):
        return sorted(files, key=lambda vf: (vf.size_bytes, vf.path.name, str(vf.path)))

    if mode == "size-desc":
        return sorted(files, key=lambda vf: (-vf.size_bytes, vf.path.name, str(vf.path)))

    if mode == "ext":
        if not extensions:
            raise ValueError("queue_sort 'ext' requires a non-empty extensions list.")
        ext_order = {ext.lower(): idx for idx, ext in enumerate(extensions)}
        return sorted(
            files,
            key=lambda vf: (
                ext_order.get(vf.path.suffix.lower(), len(ext_order)),
                vf.path.name,
                str(vf.path),
            ),
        )

    if mode == "dir":
        files_by_dir = {input_dir: [] for input_dir in input_dirs}
        leftovers: List[VideoFile] = []

        for vf in files:
            matched = False
            for input_dir in input_dirs:
                try:
                    rel_path = vf.path.relative_to(input_dir)
                except ValueError:
                    continue
                files_by_dir[input_dir].append((str(rel_path), vf))
                matched = True
                break
            if not matched:
                leftovers.append(vf)

        ordered: List[VideoFile] = []
        for input_dir in input_dirs:
            entries = files_by_dir.get(input_dir, [])
            entries.sort(key=lambda item: (item[0], item[1].path.name, str(item[1].path)))
            ordered.extend(vf for _, vf in entries)

        if leftovers:
            ordered.extend(sorted(leftovers, key=lambda vf: (vf.path.name, str(vf.path))))

        return ordered

    if mode == "rand":
        ordered = sorted(files, key=lambda vf: (vf.path.name, str(vf.path)))
        rng = random.Random(config.queue_seed)
        rng.shuffle(ordered)
        return ordered

    allowed = ", ".join(QUEUE_SORT_CHOICES)
    raise ValueError(f"Unsupported queue_sort '{mode}'. Use one of: {allowed}.")
