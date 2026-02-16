# Configuration API

This page documents the configuration models and loader.

## Models

::: vbc.config.models
    options:
      show_source: true
      heading_level: 3

## Loader

::: vbc.config.loader
    options:
      show_source: true
      heading_level: 3

## Usage Example

```python
from pathlib import Path
from vbc.config.loader import load_config
from vbc.infrastructure.ffmpeg import extract_quality_value

# Load from default location
config = load_config(Path("conf/vbc.yaml"))

# Load from custom path
config = load_config(Path("custom.yaml"))

# Access settings
print(f"Threads: {config.general.threads}")
print(f"GPU quality: {extract_quality_value(config.gpu_encoder.common_args)}")
print(f"GPU: {config.general.gpu}")

# Validation happens when creating/parsing models
from vbc.config.models import GeneralConfig
from pydantic import ValidationError
try:
    GeneralConfig(threads=0)
except ValidationError as e:
    print(e)
```

## Configuration Validation

Pydantic models provide automatic validation:

```python
from vbc.config.models import GeneralConfig
from pydantic import ValidationError

# Valid config
config = GeneralConfig(threads=8)

# Invalid: threads must be > 0
try:
    config = GeneralConfig(threads=0)
except ValidationError as e:
    print(e)
    # Field required to be greater than 0

# Invalid: min_compression_ratio must be 0.0-1.0
try:
    config = GeneralConfig(min_compression_ratio=1.5)
except ValidationError as e:
    print(e)
    # Field required to be less than or equal to 1.0
```

## Auto-Rotation Validation

```python
from vbc.config.models import AutoRotateConfig
from pydantic import ValidationError

# Valid patterns
config = AutoRotateConfig(patterns={
    "DJI_.*\\.MP4": 180,
    "GOPR.*": 90
})

# Invalid: angle must be 0, 90, 180, or 270
try:
    config = AutoRotateConfig(patterns={
        "pattern": 45  # Invalid angle
    })
except ValidationError as e:
    print(e)
    # Invalid rotation angle 45. Must be 0, 90, 180, or 270.
```
