# Contributing

Thank you for considering contributing to VBC! This document provides guidelines for development.

## Development Setup

### Prerequisites

- Python 3.12+
- `uv` package manager
- FFmpeg (for testing)
- ExifTool (optional, for metadata testing)

### Clone and Setup

```bash
# Clone repository
git clone git@github.com:your-org/vbc.git
cd vbc

# Install dependencies
uv sync

# Install documentation dependencies
uv sync --extra docs

# Verify installation
uv run vbc --help
```

### Project Structure

```
vbc/
├── config/         # Configuration (Pydantic models)
├── domain/         # Core business logic (models, events)
├── infrastructure/ # External adapters (FFmpeg, ExifTool, etc.)
├── pipeline/       # Processing coordinator (Orchestrator)
└── ui/            # User interface (Rich dashboard)
```

## Coding Standards

### Python Style

Follow **PEP 8** with these additions:

- **Line length**: 120 characters (not 79)
- **Imports**: Grouped (stdlib, third-party, local)
- **Type hints**: Required for all public functions
- **Docstrings**: Google style for all public classes/methods

**Example:**
```python
from typing import Optional, List
from pathlib import Path

from pydantic import BaseModel

from vbc.domain.models import VideoFile

def process_files(
    files: List[VideoFile],
    output_dir: Path,
    quality: int = 45
) -> Optional[int]:
    """Process video files and return count of successful compressions.

    Args:
        files: List of VideoFile objects to process
        output_dir: Directory for output files
        quality: Constant quality value (0-63)

    Returns:
        Number of successfully compressed files, or None if error

    Raises:
        ValueError: If quality is out of range
    """
    if not 0 <= quality <= 63:
        raise ValueError(f"Quality must be 0-63, got {quality}")

    success_count = 0
    for file in files:
        # ... processing logic
        success_count += 1

    return success_count
```

### Pydantic Models

Use Pydantic for all configuration and data models:

```python
from pydantic import BaseModel, Field, field_validator

class MyConfig(BaseModel):
    threads: int = Field(default=4, gt=0, le=16)
    quality: int = Field(default=45, ge=0, le=63)

    @field_validator('threads')
    @classmethod
    def validate_threads(cls, v: int) -> int:
        if v < 1:
            raise ValueError("Threads must be positive")
        return v
```

### Event-Driven Design

Always use EventBus for cross-component communication:

```python
from vbc.domain.events import Event
from pydantic import BaseModel

# Define event
class MyCustomEvent(Event):
    message: str
    count: int

# Publish
bus.publish(MyCustomEvent(message="Hello", count=42))

# Subscribe
def handler(event: MyCustomEvent):
    print(f"Received: {event.message} (count={event.count})")

bus.subscribe(MyCustomEvent, handler)
```

### Dependency Injection

Inject dependencies via constructor (no global state):

```python
# Good
class MyService:
    def __init__(self, event_bus: EventBus, config: AppConfig):
        self.event_bus = event_bus
        self.config = config

# Bad
class MyService:
    def __init__(self):
        self.event_bus = GLOBAL_EVENT_BUS  # Avoid global state
```

## Making Changes

### Workflow

1. **Create branch**: `git checkout -b feature/my-feature`
2. **Make changes**: Follow coding standards
3. **Add tests**: See [Testing](testing.md)
4. **Update docs**: Update relevant .md files
5. **Commit**: Use conventional commits (see below)
6. **Push**: `git push origin feature/my-feature`
7. **Pull request**: Create PR with description

### Conventional Commits

Use semantic commit messages:

```bash
# Features
git commit -m "feat: add support for H.265 encoding"

# Fixes
git commit -m "fix: handle corrupted EXIF data gracefully"

# Documentation
git commit -m "docs: update CLI reference with new flags"

# Refactoring
git commit -m "refactor: extract metadata caching to separate class"

# Tests
git commit -m "test: add unit tests for FFmpegAdapter"

# Chores
git commit -m "chore: update dependencies to latest versions"
```

**Format:**
```
<type>(<scope>): <subject>

<body>

<footer>
```

**Types:**
- `feat`: New feature
- `fix`: Bug fix
- `docs`: Documentation only
- `style`: Formatting (no code change)
- `refactor`: Code restructuring
- `test`: Add/update tests
- `chore`: Tooling/dependencies

## Areas for Contribution

### High Priority

1. **Unit tests**: See [Testing](testing.md)
2. **Integration tests**: End-to-end workflow tests
3. **Documentation**: Improve examples, add tutorials
4. **Performance**: Profiling and optimization

### Feature Ideas

1. **New encoders**: H.265, VP9, etc.
2. **Cloud storage**: S3, Google Drive integration
3. **Webhooks**: Notify on completion
4. **Resume from crash**: Persist queue state
5. **Batch presets**: Templates for common scenarios
6. **Web UI**: Browser-based dashboard

### Known Issues

See [GitHub Issues](https://github.com/your-org/vbc/issues) for current bugs and feature requests.

## Testing

See [Testing Guide](testing.md) for detailed testing instructions.

**Quick test:**
```bash
# Run all tests
uv run pytest

# Run with coverage
uv run pytest --cov=vbc --cov-report=html

# Run specific test
uv run pytest tests/test_config.py::test_load_config
```

## Documentation

### Build Locally

```bash
# Install docs dependencies
uv sync --extra docs

# Serve docs locally
uv run mkdocs serve

# Open browser: http://127.0.0.1:8000
```

### Adding Pages

1. Create `.md` file in `docs/`
2. Update `nav` in `mkdocs.yml`
3. Rebuild: `uv run mkdocs build`

### API Documentation

API docs are auto-generated from docstrings using `mkdocstrings`.

**Add new module:**
```markdown
# docs/api/my_module.md

::: vbc.my_module
    options:
      show_source: true
      heading_level: 3
```

## Pull Request Guidelines

### Before Submitting

- [ ] Code follows style guide
- [ ] Tests pass (`uv run pytest`)
- [ ] Documentation updated
- [ ] Commit messages follow conventional format
- [ ] No merge conflicts with main

### PR Description Template

```markdown
## Summary
Brief description of changes.

## Motivation
Why is this change needed?

## Changes
- Added X feature
- Fixed Y bug
- Refactored Z module

## Testing
How did you test this?

## Screenshots (if UI changes)
[Attach screenshots]

## Breaking Changes
List any breaking changes and migration steps.

## Checklist
- [ ] Tests pass
- [ ] Docs updated
- [ ] No breaking changes (or documented)
```

## Code Review Process

1. **Submit PR**: Wait for maintainer review
2. **Address feedback**: Make requested changes
3. **Approval**: Maintainer approves
4. **Merge**: Squash and merge to main

**Review criteria:**
- Code quality (readability, maintainability)
- Test coverage (new code should have tests)
- Documentation (public APIs documented)
- Performance (no obvious inefficiencies)
- Security (no vulnerabilities)

## Release Process

Releases are handled by maintainers:

1. Update `pyproject.toml` version
2. Update CHANGELOG.md
3. Tag release: `git tag v1.2.3`
4. Push: `git push --tags`

## Getting Help

- **Questions**: Open a [Discussion](https://github.com/your-org/vbc/discussions)
- **Bugs**: Open an [Issue](https://github.com/your-org/vbc/issues)
- **Chat**: Join our Discord/Slack (link)

## License

By contributing, you agree that your contributions will be licensed under the same license as the project (see LICENSE file).

## Code of Conduct

Be respectful and constructive. We follow the [Contributor Covenant](https://www.contributor-covenant.org/).

## Attribution

Contributors are listed in `CONTRIBUTORS.md` (automatically updated from git log).