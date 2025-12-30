# VBC Documentation

This directory contains the source files for VBC documentation, built with **MkDocs** and **Material for MkDocs**.

## Building Documentation

### Install Dependencies

```bash
# Install all dependencies including docs
uv sync --extra docs

# Or with pip
pip install -e ".[docs]"
```

### Build Static Site

```bash
# Build documentation to site/
uv run mkdocs build

# Built files will be in site/ directory
```

### Serve Locally

```bash
# Start development server with live reload
uv run mkdocs serve

# Open browser at http://127.0.0.1:8000
```

### Deploy to GitHub Pages

```bash
# Build and deploy to gh-pages branch
uv run mkdocs gh-deploy
```

## Documentation Structure

```
docs/
├── index.md                 # Homepage
├── getting-started/         # Installation, quickstart, configuration
├── architecture/            # Design documentation
├── user-guide/              # CLI reference, runtime controls, advanced features
├── api/                     # Auto-generated API reference
└── development/             # Contributing, testing, migration guides
```

## Adding Pages

1. Create `.md` file in appropriate directory
2. Add entry to `nav` section in `mkdocs.yml`
3. Rebuild: `uv run mkdocs build`

## API Documentation

API reference is auto-generated from docstrings using `mkdocstrings`.

**To add a new module:**

```markdown
# docs/api/my_module.md

::: vbc.my_module
    options:
      show_source: true
      heading_level: 3
```

## Features

- **Material Theme**: Modern, responsive design
- **Search**: Full-text search across all pages
- **Code Highlighting**: Syntax highlighting for Python, Bash, YAML
- **Auto-generated API**: API docs from docstrings (Google style)
- **Navigation**: Tabbed navigation, expandable sections
- **Dark Mode**: Automatic light/dark mode switching

## Requirements

- Python 3.12+
- mkdocs >= 1.5.3
- mkdocs-material >= 9.5.0
- mkdocstrings[python] >= 0.24.0
- pymdown-extensions >= 10.7

See `pyproject.toml` for exact versions.

## Troubleshooting

### "Could not collect module"

If mkdocstrings can't find modules, ensure:

1. VBC is installed in editable mode: `uv pip install -e .`
2. All packages have `__init__.py` files
3. `paths: [.]` is set in mkdocs.yml

### "Black or Ruff not installed"

This is a warning, not an error. Signature formatting will work without Black/Ruff, just with slightly different formatting.

To suppress:
```bash
uv add --dev black
# or
uv add --dev ruff
```

## License

Documentation is part of the VBC project and follows the same license.
