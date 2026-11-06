from pathlib import Path

import asyncio
import sys

import pytest

from claimsleuth.dataset import build_dataset


@pytest.fixture(scope="session")
def dataset(tmp_path_factory) -> tuple[Path, Path]:
    """A freshly built copy of the synthetic dataset, so tests never touch the repo's data folder."""
    data_dir = tmp_path_factory.mktemp("data")
    build_dataset(data_dir)
    return data_dir, data_dir / "claimsleuth.db"


@pytest.fixture(scope="session")
def evaluation(tmp_path_factory):
    """One real 40-case OFFLINE evaluation per test session (labelled test doubles, its own MCP and CrewAI services,
    its own blackboard and a temporary request ledger), shared by the regression tests that need it."""
    crew_python = Path(__file__).resolve().parents[1] / ".venv-crew" / (
        "Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    if not crew_python.exists():
        pytest.skip(".venv-crew is needed for the CrewAI service")
    from claimsleuth.evaluation.runner import run_evaluation
    from test_mcp_server import _free_port
    tmp = tmp_path_factory.mktemp("eval40")
    mp = pytest.MonkeyPatch()
    mp.setenv("CLAIMSLEUTH_GEMINI_LEDGER", str(tmp / "ledger.sqlite3"))
    mp.delenv("EXECUTION_MODE", raising=False)
    mp.delenv("GOOGLE_API_KEY", raising=False)
    try:
        result = asyncio.run(run_evaluation("offline", root=tmp / "evaluation", mcp_port=_free_port(),
                                            crew_port=_free_port()))
    finally:
        mp.undo()
    return result, tmp
