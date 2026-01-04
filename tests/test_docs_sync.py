"""
Tests to ensure documentation stays in sync with code.

These tests detect drift between:
- CLI flags in main.py and docs/user-guide/cli.md
- Config fields in config/models.py and docs/getting-started/configuration.md
- Events in domain/events.py and docs/architecture/events.md
"""

import re
from pathlib import Path
import pytest
from vbc.config.models import GeneralConfig, GpuConfig, UiConfig, AppConfig, AutoRotateConfig
from vbc.domain import events
from vbc.ui import keyboard


def read_file(relative_path: str) -> str:
    """Read a file relative to the project root."""
    root = Path(__file__).parent.parent
    return (root / relative_path).read_text()


class TestCliDocsSync:
    """Verify CLI flags are documented."""

    def test_main_cli_flags_are_documented(self):
        """All CLI flags in main.py should appear in cli.md"""
        # Read main.py and extract CLI flag definitions
        main_content = read_file("vbc/main.py")

        # Extract option names from typer.Option calls
        cli_flags = set()

        # Match: typer.Option(..., "--flag-name", ...)
        for match in re.finditer(r'typer\.Option\([^,]+,\s*"--([^"]+)"', main_content):
            flag_name = match.group(1)
            # Skip boolean negations like "--no-debug" - just use base flag
            if not flag_name.startswith("no-"):
                # For boolean flags like "--gpu/--cpu", extract just the primary flag
                if "/" not in flag_name:
                    cli_flags.add(flag_name)
                else:
                    # Take first part before /
                    cli_flags.add(flag_name.split("/")[0])

        # Also extract short flags like -c, -t
        for match in re.finditer(r'typer\.Option\([^,]+,\s*"--[^"]+",\s*"-([^"]+)"', main_content):
            cli_flags.add(match.group(1))

        # Read cli.md
        cli_docs = read_file("docs/user-guide/cli.md")

        # Check each flag is documented
        missing_flags = []
        for flag in cli_flags:
            # Look for flag in headers like "#### `--flag-name`" or mentions in text
            if f"--{flag}" not in cli_docs and f"-{flag}" not in cli_docs:
                missing_flags.append(flag)

        assert not missing_flags, f"CLI flags not documented in cli.md: {missing_flags}"

    def test_demo_flag_documented(self):
        """Demo mode should be documented correctly"""
        cli_docs = read_file("docs/user-guide/cli.md")
        assert "--demo" in cli_docs, "Demo flag not documented"


class TestConfigDocsSync:
    """Verify config fields are documented."""

    def test_general_config_fields_documented(self):
        """All GeneralConfig fields should appear in configuration.md"""
        config_docs = read_file("docs/getting-started/configuration.md")

        # Get all field names from GeneralConfig
        general_fields = set(GeneralConfig.model_fields.keys())

        # Check each field is documented
        missing_fields = []
        for field in general_fields:
            # Convert field_name to section anchor (both formats)
            field_md1 = f"`{field}`"
            field_md2 = f"#### `{field}`"

            if field_md1 not in config_docs and field_md2 not in config_docs:
                missing_fields.append(field)

        assert not missing_fields, f"GeneralConfig fields not documented: {missing_fields}"

    def test_gpu_config_fields_documented(self):
        """All GpuConfig fields should appear in configuration.md"""
        config_docs = read_file("docs/getting-started/configuration.md")

        # Get all field names from GpuConfig
        gpu_fields = set(GpuConfig.model_fields.keys())

        # Check each field is documented
        missing_fields = []
        for field in gpu_fields:
            field_md1 = f"`{field}`"
            field_md2 = f"#### `{field}`"

            if field_md1 not in config_docs and field_md2 not in config_docs:
                missing_fields.append(field)

        assert not missing_fields, f"GpuConfig fields not documented: {missing_fields}"

    def test_ui_config_fields_documented(self):
        """All UiConfig fields should appear in configuration.md"""
        config_docs = read_file("docs/getting-started/configuration.md")

        ui_fields = set(UiConfig.model_fields.keys())

        missing_fields = []
        for field in ui_fields:
            field_md1 = f"`{field}`"
            field_md2 = f"#### `{field}`"

            if field_md1 not in config_docs and field_md2 not in config_docs:
                missing_fields.append(field)

        assert not missing_fields, f"UiConfig fields not documented: {missing_fields}"

    def test_threads_default_value_matches(self):
        """threads default in docs should match code"""
        config_docs = read_file("docs/getting-started/configuration.md")

        # Get actual default from Pydantic model
        threads_field = GeneralConfig.model_fields["threads"]
        actual_default = threads_field.default

        # Check if docs mention the correct default
        # Look for "**Default**: 1" in the threads section
        assert f"**Default**: {actual_default}" in config_docs, \
            f"threads default in docs doesn't match code (expected {actual_default})"


class TestEventsDocsSync:
    """Verify events are documented."""

    def test_domain_events_documented(self):
        """All domain events should appear in events.md"""
        events_docs = read_file("docs/architecture/events.md")

        # Get all event classes from domain/events.py
        domain_event_classes = []
        for name in dir(events):
            obj = getattr(events, name)
            if (isinstance(obj, type) and
                issubclass(obj, events.Event) and
                obj is not events.Event and
                obj is not events.JobEvent):
                domain_event_classes.append(name)

        # Check each event is documented
        missing_events = []
        for event_name in domain_event_classes:
            if event_name not in events_docs:
                missing_events.append(event_name)

        assert not missing_events, f"Domain events not documented: {missing_events}"

    def test_keyboard_events_mentioned(self):
        """UI/keyboard events should be mentioned in events.md with location info"""
        events_docs = read_file("docs/architecture/events.md")

        # Key UI events that should be documented
        ui_events = ["ThreadControlEvent", "RequestShutdown", "InterruptRequested"]

        missing_events = []
        for event_name in ui_events:
            if event_name not in events_docs:
                missing_events.append(event_name)

        assert not missing_events, f"UI/keyboard events not mentioned: {missing_events}"

        # Check that location is mentioned
        assert "vbc/ui/keyboard.py" in events_docs, \
            "Events docs should mention vbc/ui/keyboard.py as event location"


class TestCanonicalInvocation:
    """Verify consistent command invocation across docs."""

    def test_canonical_form_is_uv_run_vbc(self):
        """All docs should use 'uv run vbc' not 'uv run vbc/main.py'"""
        docs_to_check = [
            "README.md",
            "docs/index.md",
            "docs/getting-started/quickstart.md",
            "docs/getting-started/configuration.md",
            "docs/user-guide/cli.md",
            "CLAUDE.md",
        ]

        issues = []
        for doc_path in docs_to_check:
            content = read_file(doc_path)
            # Check for non-canonical form
            if "uv run vbc/main.py" in content:
                # Count occurrences
                count = content.count("uv run vbc/main.py")
                issues.append(f"{doc_path}: {count} occurrence(s) of 'uv run vbc/main.py'")

        assert not issues, f"Non-canonical invocations found:\n" + "\n".join(issues)


class TestDeadLinks:
    """Verify no dead links in documentation."""

    def test_mkdocs_nav_files_exist(self):
        """All files referenced in mkdocs.yml navigation should exist"""
        mkdocs_content = read_file("mkdocs.yml")
        root = Path(__file__).parent.parent

        # Extract all .md file references from nav section
        doc_files = re.findall(r':\s*([a-z\-/]+\.md)', mkdocs_content)

        missing_files = []
        for doc_file in doc_files:
            doc_path = root / "docs" / doc_file
            if not doc_path.exists():
                missing_files.append(doc_file)

        assert not missing_files, f"Referenced docs don't exist: {missing_files}"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
