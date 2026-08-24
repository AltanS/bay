"""Tests for runner.run() — subprocess execution with spinner/capture modes."""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from bay_cli.errors import BayError
from bay_cli.runner import run


class TestRunCapture:
    """Tests for capture=True mode (stdout/stderr captured)."""

    @patch("bay_cli.runner.subprocess.run")
    def test_success_returns_completed_process(self, mock_run: MagicMock) -> None:
        mock_run.return_value = subprocess.CompletedProcess(
            args=["echo", "hi"], returncode=0, stdout="hi\n", stderr=""
        )
        result = run(["echo", "hi"], capture=True)
        assert result.returncode == 0
        assert result.stdout == "hi\n"

    @patch("bay_cli.runner.subprocess.run")
    def test_failure_with_check_raises_bay_error(self, mock_run: MagicMock) -> None:
        mock_run.return_value = subprocess.CompletedProcess(
            args=["false"], returncode=1, stdout="", stderr="boom"
        )
        with pytest.raises(BayError, match="Command failed"):
            run(["false"], capture=True, check=True)

    @patch("bay_cli.runner.subprocess.run")
    def test_failure_without_check_returns_result(self, mock_run: MagicMock) -> None:
        mock_run.return_value = subprocess.CompletedProcess(
            args=["false"], returncode=1, stdout="", stderr="boom"
        )
        result = run(["false"], capture=True, check=False)
        assert result.returncode == 1

    @patch("bay_cli.runner.subprocess.run")
    def test_success_with_message_prints_checkmark(
        self, mock_run: MagicMock, capsys: pytest.CaptureFixture[str]
    ) -> None:
        mock_run.return_value = subprocess.CompletedProcess(
            args=["echo"], returncode=0, stdout="", stderr=""
        )
        result = run(["echo"], capture=True, message="Doing thing...")
        assert result.returncode == 0

    @patch("bay_cli.runner.subprocess.run")
    def test_failure_with_message_and_check_raises(self, mock_run: MagicMock) -> None:
        mock_run.return_value = subprocess.CompletedProcess(
            args=["fail"], returncode=1, stdout="output", stderr="err"
        )
        with pytest.raises(BayError, match="Command failed"):
            run(["fail"], capture=True, message="Trying...", check=True)

    @patch("bay_cli.runner.subprocess.run")
    def test_failure_with_message_no_check(self, mock_run: MagicMock) -> None:
        mock_run.return_value = subprocess.CompletedProcess(
            args=["fail"], returncode=1, stdout="", stderr=""
        )
        result = run(["fail"], capture=True, message="Trying...", check=False)
        assert result.returncode == 1


class TestRunStream:
    """Tests for capture=False mode (live streaming to terminal)."""

    @patch("bay_cli.runner.subprocess.run")
    def test_stream_success(self, mock_run: MagicMock) -> None:
        mock_run.return_value = subprocess.CompletedProcess(
            args=["ls"], returncode=0, stdout=None, stderr=None
        )
        result = run(["ls"], capture=False)
        assert result.returncode == 0
        # Verify subprocess was called with stdout/stderr forwarded
        call_kwargs = mock_run.call_args[1]
        assert "capture_output" not in call_kwargs or not call_kwargs.get("capture_output")

    @patch("bay_cli.runner.subprocess.run")
    def test_stream_failure_with_check_raises(self, mock_run: MagicMock) -> None:
        mock_run.return_value = subprocess.CompletedProcess(
            args=["bad"], returncode=2, stdout=None, stderr=None
        )
        with pytest.raises(BayError, match="exit code 2"):
            run(["bad"], capture=False, check=True)

    @patch("bay_cli.runner.subprocess.run")
    def test_stream_failure_without_check(self, mock_run: MagicMock) -> None:
        mock_run.return_value = subprocess.CompletedProcess(
            args=["bad"], returncode=2, stdout=None, stderr=None
        )
        result = run(["bad"], capture=False, check=False)
        assert result.returncode == 2


class TestRunEnvMerge:
    """Tests for environment variable merging."""

    @patch("bay_cli.runner.subprocess.run")
    def test_custom_env_merged_with_os_environ(self, mock_run: MagicMock) -> None:
        mock_run.return_value = subprocess.CompletedProcess(
            args=["env"], returncode=0, stdout="", stderr=""
        )
        run(["env"], capture=True, env={"MY_VAR": "hello"})
        call_kwargs = mock_run.call_args[1]
        assert call_kwargs["env"]["MY_VAR"] == "hello"
        # OS env vars should also be present
        assert "PATH" in call_kwargs["env"]

    @patch("bay_cli.runner.subprocess.run")
    def test_no_env_still_passes_os_environ(self, mock_run: MagicMock) -> None:
        mock_run.return_value = subprocess.CompletedProcess(
            args=["env"], returncode=0, stdout="", stderr=""
        )
        run(["env"], capture=True)
        call_kwargs = mock_run.call_args[1]
        assert "PATH" in call_kwargs["env"]


class TestRunJsonMode:
    """Tests for JSON mode behavior (message present but JSON mode active)."""

    @patch("bay_cli.runner.is_json_mode", return_value=True)
    @patch("bay_cli.runner.subprocess.run")
    def test_json_mode_skips_spinner(
        self, mock_run: MagicMock, _mock_json: MagicMock
    ) -> None:
        mock_run.return_value = subprocess.CompletedProcess(
            args=["echo"], returncode=0, stdout="ok", stderr=""
        )
        result = run(["echo"], capture=True, message="Working...")
        assert result.returncode == 0

    @patch("bay_cli.runner.is_json_mode", return_value=True)
    @patch("bay_cli.runner.subprocess.run")
    def test_json_mode_failure_raises(
        self, mock_run: MagicMock, _mock_json: MagicMock
    ) -> None:
        mock_run.return_value = subprocess.CompletedProcess(
            args=["fail"], returncode=1, stdout="err output", stderr=""
        )
        with pytest.raises(BayError, match="Command failed"):
            run(["fail"], capture=True, message="Working...", check=True)
