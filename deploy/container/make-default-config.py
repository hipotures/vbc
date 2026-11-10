"""Build the container's first-start config from conf/vbc.yaml.example.

Keeps every encoder/quality/autorotate default of the example (so the image
behaves like a local `vbc serve` with that file) and changes only what a headless
container needs. Used at image build time:
    python make-default-config.py conf/vbc.yaml.example conf/container-default.yaml
"""

import sys
from pathlib import Path

import yaml

source, target = map(Path, sys.argv[1:3])
config = yaml.safe_load(source.read_text())
config["input_dirs"] = []  # added at runtime through the API (`vbc dirs add`)
general = config["general"]
general["gpu"] = True  # GPU (NVENC) when usable, else CPU: `vbc serve` probes av1_nvenc at start
general["log_path"] = None  # `vbc serve` logs to stdout
general["wait_on_finish"] = True
general["bell_on_finish"] = False
config["gpu_config"]["enabled"] = True  # nvtop samples only while general.gpu is true
config["web_server"]["enabled"] = False
config["server"] = {"host": "0.0.0.0", "port": 8080, "allowed_hosts": [], "stop_timeout_s": 600}
config["storage"] = {"roots": []}  # set by the VBC_STORAGE_ROOTS deployment input
target.write_text(
    "# First-start configuration of the VBC container (generated from vbc.yaml.example).\n"
    "# Lives on the persistent volume; edit freely, the image never overwrites it.\n"
    + yaml.safe_dump(config, sort_keys=False, allow_unicode=True)
)
