"""Deploy flags placed AFTER the env positional must still take effect.

`deploy` is registered with allow_interspersed_args=False, so Click stops
parsing options at the positional `env` and dumps everything after it into
ctx.args. Declared Typer options therefore keep their default unless deploy
explicitly promotes them back.

`--check-token-scope` was declared, and filtered out of the ansible passthrough,
but never promoted — so `bin/bay deploy production --check-token-scope` was
silently DROPPED: it reached neither run_validation nor ansible. The operator
asked for a token-scope check, saw a clean validation, and got no check. Worse
than an error, because it looks like a pass.

`--skip-healthcheck` has always been promoted; it is covered here to keep the
pair honest.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from bay_cli.commands.ops import _rescue_interspersed_args


class TestFlagsSurviveAfterEnv:
    """The passthrough filter must not be the only thing that sees these flags.

    ops.py filters --check-token-scope/--skip-healthcheck out of extra_args, so
    an unpromoted flag vanishes silently rather than reaching ansible. Assert
    the promotion checks exist and read from ctx.args.
    """

    @pytest.mark.parametrize("flag", ["--check-token-scope", "--skip-healthcheck"])
    def test_flag_is_promoted_from_ctx_args(self, flag: str) -> None:
        import inspect

        from bay_cli.commands import ops

        src = inspect.getsource(ops.deploy)
        assert f'"{flag}" in (ctx.args or [])' in src, (
            f"{flag} is filtered out of extra_args but never promoted — placed "
            "after env it is silently dropped, not forwarded"
        )

    @pytest.mark.parametrize("flag", ["--check-token-scope", "--skip-healthcheck"])
    def test_flag_is_still_filtered_from_ansible_passthrough(self, flag: str) -> None:
        """Promotion must not start leaking bay-only flags to ansible-playbook."""
        import inspect
        import re

        from bay_cli.commands import ops

        src = inspect.getsource(ops.deploy)
        m = re.search(r"if a not in \(([^)]*)\)", src)
        assert m, "extra_args passthrough filter not found in deploy()"
        assert f'"{flag}"' in m.group(1), (
            f"{flag} must stay filtered out of extra_args — ansible-playbook "
            "does not know it and would exit 2"
        )


class TestRescueLeavesBayOnlyFlagsForDeploy:
    """_rescue_interspersed_args must not eat the flags deploy promotes itself.

    It handles --rig/--tags/--region/--skip-validate; --check-token-scope and
    --skip-healthcheck are promoted by deploy afterwards by reading ctx.args, so
    rescue has to leave them in place.
    """

    @patch("bay_cli.commands.ops.console")
    def test_check_token_scope_survives_rescue(self, _console: MagicMock) -> None:
        ctx = MagicMock()
        ctx.args = ["--check-token-scope"]
        _rescue_interspersed_args(ctx, rig=False, tags=None)
        assert "--check-token-scope" in ctx.args

    @patch("bay_cli.commands.ops.console")
    def test_skip_healthcheck_survives_rescue(self, _console: MagicMock) -> None:
        ctx = MagicMock()
        ctx.args = ["--skip-healthcheck"]
        _rescue_interspersed_args(ctx, rig=False, tags=None)
        assert "--skip-healthcheck" in ctx.args

    @patch("bay_cli.commands.ops.console")
    def test_separator_stripped_but_bay_flags_kept(self, _console: MagicMock) -> None:
        """`deploy production -- --check-token-scope` — separator goes, flag stays."""
        ctx = MagicMock()
        ctx.args = ["--", "--check-token-scope", "--check"]
        _rescue_interspersed_args(ctx, rig=False, tags=None)
        assert ctx.args == ["--check-token-scope", "--check"]
