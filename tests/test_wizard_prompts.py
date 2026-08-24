"""Tests for the interactive wizard prompt flow (bay_cli.wizard.prompts).

Uses monkeypatching to simulate questionary and Rich prompt responses
without a real terminal. Each test builds mock objects for
questionary.select, questionary.checkbox, and Rich Prompt.ask, then
verifies the resulting WizardResult.

Note: _prompt_confirm uses questionary.select (Yes/No choices), so all
confirm responses are interleaved into select_responses in call order.
"""

from __future__ import annotations

import pytest

from bay_cli.catalog import _package_framework_root, load_catalog
from bay_cli.wizard.models import WizardResult
from bay_cli.wizard.prompts import _get_catalog, run_wizard


# ── Mock Helpers ────────────────────────────────────────────────────────


class _MockQuestion:
    """A mock questionary Question that returns a preset value from .ask()."""

    def __init__(self, value: object) -> None:
        self._value = value

    def ask(self) -> object:
        return self._value


def _mock_select_factory(values: list) -> object:
    """Return a callable that replaces questionary.select with preset values."""
    it = iter(values)

    def _mock_select(*args, **kwargs):  # noqa: ARG001
        return _MockQuestion(next(it))

    return _mock_select


def _mock_checkbox_factory(values: list[list[str]]) -> object:
    """Return a callable that replaces questionary.checkbox with preset lists."""
    it = iter(values)

    def _mock_checkbox(*args, **kwargs):  # noqa: ARG001
        return _MockQuestion(next(it))

    return _mock_checkbox


def _mock_text_factory(values: list[str]) -> object:
    """Return a callable that replaces questionary.text with preset strings."""
    it = iter(values)

    def _mock_text(*args, **kwargs):  # noqa: ARG001
        return _MockQuestion(next(it))

    return _mock_text


def _setup_wizard_mocks(
    monkeypatch,
    *,
    prompt_responses: list,
    select_responses: list,
    checkbox_responses: list[list[str]],
    text_responses: list[str] | None = None,
) -> None:
    """Wire up all mocks needed for a wizard run.

    Note: _prompt_confirm uses questionary.select (Yes/No arrow-key picker),
    so confirm values (True/False) must be interleaved into select_responses
    in the order they are called during the wizard flow.
    """
    prompt_it = iter(prompt_responses)
    monkeypatch.setattr(
        "bay_cli.wizard.prompts.Prompt.ask",
        lambda *a, **kw: next(prompt_it),
    )
    monkeypatch.setattr(
        "bay_cli.wizard.prompts.questionary.select",
        _mock_select_factory(select_responses),
    )
    monkeypatch.setattr(
        "bay_cli.wizard.prompts.questionary.checkbox",
        _mock_checkbox_factory(checkbox_responses),
    )
    if text_responses is not None:
        monkeypatch.setattr(
            "bay_cli.wizard.prompts.questionary.text",
            _mock_text_factory(text_responses),
        )


# ── Single-server flow ───────────────────────────────────────────────────


class TestSingleServerFlow:
    """Simulate a complete single-server wizard run."""

    def test_single_server_headscale(self, monkeypatch, tmp_path) -> None:
        # Create .vault_pass so the vault step is skipped
        (tmp_path / ".vault_pass").write_text("test")
        monkeypatch.chdir(tmp_path)

        _setup_wizard_mocks(
            monkeypatch,
            # Prompt.ask calls (text input):
            #  1. project name -> "myapp"
            #  2. server IP -> "10.0.0.1"
            #  3. domain -> "example.com"
            #  4. LE email -> "admin@example.com"
            #  5. headscale domain -> "hs.example.com"
            prompt_responses=[
                "myapp",              # project name
                "10.0.0.1",           # server IP
                "example.com",        # domain
                "admin@example.com",  # LE email
                "hs.example.com",     # headscale domain
            ],
            # questionary.select calls (includes confirms as Yes/No selects):
            #  1. multi-region -> False (confirm)
            #  2. SSH key choice -> "skip"
            #  3. gateway -> "headscale"
            #  4. scaffold confirm -> True (confirm)
            select_responses=[False, "skip", "headscale", True],
            # questionary.checkbox calls:
            #  1. services -> ["gatus"]
            checkbox_responses=[["gatus"]],
        )

        result = run_wizard()

        assert isinstance(result, WizardResult)
        assert result.project_name == "myapp"
        assert result.multi_region is False
        assert result.server_ip == "10.0.0.1"
        assert result.domain_base == "example.com"
        assert result.access_gateway == "headscale"
        assert result.headscale_domain == "hs.example.com"
        assert result.vpn_enabled is True
        assert result.selected_services == ["gatus"]

    def test_single_server_none_gateway(self, monkeypatch, tmp_path) -> None:
        (tmp_path / ".vault_pass").write_text("test")
        monkeypatch.chdir(tmp_path)

        _setup_wizard_mocks(
            monkeypatch,
            prompt_responses=[
                "myapp", "10.0.0.1", "example.com", "admin@example.com",
            ],
            # multi-region(False), SSH skip, gateway none, scaffold confirm(True)
            select_responses=[False, "skip", "none", True],
            checkbox_responses=[["gatus"]],
        )

        result = run_wizard()
        assert result.access_gateway == "none"
        assert result.vpn_enabled is False
        assert result.headscale_domain is None


# ── Multi-region flow ────────────────────────────────────────────────────


class TestMultiRegionFlow:
    """Simulate a multi-region wizard run with 2 regions."""

    def test_multi_region_produces_correct_result(self, monkeypatch, tmp_path) -> None:
        (tmp_path / ".vault_pass").write_text("test")
        monkeypatch.chdir(tmp_path)

        _setup_wizard_mocks(
            monkeypatch,
            # Prompt.ask calls:
            #  1. project name -> "multi-app"
            #  2. region 1 name -> "eu"
            #  3. region 1 IP -> "10.0.0.1"
            #  4. region 2 name -> "na"
            #  5. region 2 IP -> "10.0.0.2"
            #  6. domain -> "example.com"
            #  7. LE email -> "ops@example.com"
            #  8. headscale domain -> "hs.example.com"
            prompt_responses=[
                "multi-app",
                "eu", "10.0.0.1",
                "na", "10.0.0.2",
                "example.com",
                "ops@example.com",
                "hs.example.com",
            ],
            # multi-region(True), SSH skip, gateway headscale, primary region(eu), scaffold confirm(True)
            select_responses=[True, "skip", "headscale", "eu", True],
            # questionary.checkbox calls:
            #  1. services -> ["postgres"]
            checkbox_responses=[["postgres"]],
            # questionary.text calls:
            #  1. region count -> "2"
            text_responses=["2"],
        )

        result = run_wizard()

        assert isinstance(result, WizardResult)
        assert result.project_name == "multi-app"
        assert result.multi_region is True
        assert result.regions is not None
        assert len(result.regions) == 2
        assert result.regions[0].name == "eu"
        assert result.regions[0].server_ip == "10.0.0.1"
        assert result.regions[1].name == "na"
        assert result.regions[1].server_ip == "10.0.0.2"
        assert result.domain_base == "example.com"
        assert result.access_gateway == "headscale"
        assert result.headscale_domain == "hs.example.com"
        assert result.selected_services == ["postgres"]


# ── Dependency auto-selection ────────────────────────────────────────────


class TestDependencyAutoSelection:
    """Selecting services with dependencies auto-adds required accessories."""

    def test_plausible_auto_adds_postgres(self, monkeypatch, tmp_path) -> None:
        (tmp_path / ".vault_pass").write_text("test")
        monkeypatch.chdir(tmp_path)

        _setup_wizard_mocks(
            monkeypatch,
            prompt_responses=[
                "myapp", "10.0.0.1", "example.com", "admin@example.com",
            ],
            select_responses=[False, "skip", "none", True],
            checkbox_responses=[["plausible"]],  # checkbox returns plausible
        )

        result = run_wizard()
        assert "plausible" in result.selected_services
        assert "postgres" in result.selected_services

    def test_n8n_auto_adds_postgres(self, monkeypatch, tmp_path) -> None:
        (tmp_path / ".vault_pass").write_text("test")
        monkeypatch.chdir(tmp_path)

        _setup_wizard_mocks(
            monkeypatch,
            prompt_responses=[
                "myapp", "10.0.0.1", "example.com", "admin@example.com",
            ],
            select_responses=[False, "skip", "none", True],
            checkbox_responses=[["n8n"]],
        )

        result = run_wizard()
        assert "n8n" in result.selected_services
        assert "postgres" in result.selected_services


# ── Ctrl+C handling ──────────────────────────────────────────────────────


class TestCtrlCHandling:
    """KeyboardInterrupt during prompts propagates to the caller."""

    def test_keyboard_interrupt_propagates(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "bay_cli.wizard.prompts.Prompt.ask",
            lambda *a, **kw: (_ for _ in ()).throw(KeyboardInterrupt),
        )

        with pytest.raises(KeyboardInterrupt):
            run_wizard()


# ── Non-TTY guard ───────────────────────────────────────────────────────


class TestNonTTYGuard:
    """questionary returning None triggers KeyboardInterrupt via _ask_or_interrupt."""

    def test_select_none_raises(self, monkeypatch, tmp_path) -> None:
        """When questionary.select().ask() returns None, wizard raises."""
        (tmp_path / ".vault_pass").write_text("test")
        monkeypatch.chdir(tmp_path)

        # Prompt.ask returns valid values for text inputs
        prompt_it = iter(["myapp"])
        monkeypatch.setattr(
            "bay_cli.wizard.prompts.Prompt.ask",
            lambda *a, **kw: next(prompt_it),
        )
        # questionary.select returns None (non-TTY) — first call is multi-region confirm
        monkeypatch.setattr(
            "bay_cli.wizard.prompts.questionary.select",
            lambda *a, **kw: _MockQuestion(None),
        )

        with pytest.raises(KeyboardInterrupt):
            run_wizard()


# ── Validation retry ─────────────────────────────────────────────────────


class TestValidationRetry:
    """_prompt_validated re-prompts on invalid input until valid input is given."""

    def test_retries_on_invalid_then_accepts_valid(self, monkeypatch, tmp_path) -> None:
        (tmp_path / ".vault_pass").write_text("test")
        monkeypatch.chdir(tmp_path)

        _setup_wizard_mocks(
            monkeypatch,
            # The first Prompt.ask call is for project name.
            # _prompt_validated loops: first call returns invalid "", second valid "myapp".
            prompt_responses=[
                "",               # invalid project name (empty)
                "myapp",          # valid project name (retry succeeds)
                "10.0.0.1",       # server IP
                "example.com",    # domain
                "admin@example.com",  # LE email
            ],
            select_responses=[False, "skip", "none", True],
            checkbox_responses=[["gatus"]],
        )

        result = run_wizard()

        # The first empty value was rejected; the wizard used "myapp"
        assert result.project_name == "myapp"
        assert result.server_ip == "10.0.0.1"


# ── Catalog integration ────────────────────────────────────────────────


class TestCatalogIntegration:
    """Wizard service list matches load_catalog() entries."""

    def test_wizard_catalog_matches_loader(self) -> None:
        fw_root = _package_framework_root()
        catalog = load_catalog(fw_root, fw_root)
        wizard_catalog = _get_catalog()
        assert set(catalog.keys()) == set(wizard_catalog.keys())
        for entry_id, entry in catalog.items():
            assert wizard_catalog[entry_id].name == entry.name
            assert wizard_catalog[entry_id].category == entry.category
