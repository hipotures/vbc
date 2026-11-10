"""Headless server entrypoint shared by the container and ``vbc serve``."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from vbc.service.api import run_server
from vbc.service.bootstrap import ServerConfigError, VbcServer


def serve(
    config: Path,
    host: str | None = None,
    port: int | None = None,
    demo: bool = False,
    demo_config: Path = Path("conf/demo.yaml"),
    log_file: Path | None = None,
    dirs: list[Path] | None = None,
) -> int:
    """``dirs`` (non-empty) replace the config's folders for this process only."""
    if not config.exists():
        print(f"vbc serve: config file not found: {config}", file=sys.stderr)
        return 2
    dirs = [path.resolve() for path in dirs] if dirs else None
    for path in dirs or []:
        if not path.is_dir():
            print(f"vbc serve: not a directory: {path}", file=sys.stderr)
            return 2
    try:
        server = VbcServer(
            config, demo=demo, demo_config_path=demo_config if demo else None,
            log_file=log_file, host=host, port=port, input_dirs=dirs,
        )
    except ServerConfigError as exc:
        print(f"vbc serve: {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"vbc serve: invalid config {config}:\n{exc}", file=sys.stderr)
        return 2
    code = run_server(server)
    if server.restart_requested and code == 0:
        # Re-execute the headless entrypoint after a config push.
        args = [sys.executable, "-m", "vbc.service", "--config", str(config)]
        if host is not None:
            args.extend(["--host", host])
        if port is not None:
            args.extend(["--port", str(port)])
        if demo:
            args.extend(["--demo", "--demo-config", str(demo_config)])
        if log_file is not None:
            args.extend(["--log-file", str(log_file)])
        args.extend(str(path) for path in dirs or [])
        print("vbc serve: restarting with the new config", flush=True)
        os.execv(sys.executable, args)
    return code


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the headless VBC server.")
    parser.add_argument("--config", "-c", type=Path, default=Path("conf/vbc.yaml"))
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    parser.add_argument("--demo", action="store_true")
    parser.add_argument("--demo-config", type=Path, default=Path("conf/demo.yaml"))
    parser.add_argument("--log-file", type=Path)
    parser.add_argument("dirs", nargs="*", type=Path,
                        help="Compress these folders instead of the config's list (this run only, not saved)")
    return serve(**vars(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
