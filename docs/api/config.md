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

# Load from default location
config = load_config()

# Load from custom path
config = load_config(Path("custom.yaml"))

# Access settings
print(f"Threads: {config.general.threads}")
print(f"CQ: {config.general.cq}")
print(f"GPU: {config.general.gpu}")

# Validate at runtime
from pydantic import ValidationError
try:
    config.general.threads = -1  # Will raise ValidationError
except ValidationError as e:
    print(e)
```

## Configuration Validation

Pydantic models provide automatic validation:

```python
from vbc.config.models import GeneralConfig
from pydantic import ValidationError

# Valid config
config = GeneralConfig(threads=8, cq=45)

# Invalid: threads must be > 0
try:
    config = GeneralConfig(threads=0, cq=45)
except ValidationError as e:
    print(e)
    # Field required to be greater than 0

# Invalid: cq must be 0-63
try:
    config = GeneralConfig(threads=8, cq=70)
except ValidationError as e:
    print(e)
    # Field required to be between 0 and 63
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
