"""The app entry point's account guard.

`app.py` refuses to run with no account configured, so a `cdk deploy` that forgot its env vars
fails with a readable message instead of an assume-role error against a fake account thirty
seconds in. This pins both directions of that guard, running `app.py` as a subprocess with a
controlled environment — the same way the CDK CLI invokes it.
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys

_INFRA = pathlib.Path(__file__).resolve().parents[1]


def _run_app(env_overrides: dict[str, str]) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CDK_", "AQM_"))}
    env.update(env_overrides)
    return subprocess.run(
        [sys.executable, "app.py"],
        cwd=_INFRA,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_no_account_configured_fails_with_a_readable_message() -> None:
    result = _run_app({})
    assert result.returncode != 0
    assert "no account configured" in (result.stdout + result.stderr)


def test_the_synth_placeholder_opt_in_lets_it_run() -> None:
    result = _run_app({"AQM_CDK_SYNTH_PLACEHOLDER": "1"})
    assert result.returncode == 0, result.stdout + result.stderr


def test_an_explicit_account_lets_it_run() -> None:
    result = _run_app({"CDK_DEPLOY_ACCOUNT": "111122223333"})
    assert result.returncode == 0, result.stdout + result.stderr
