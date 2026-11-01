from pathlib import Path

import pytest

from claimsleuth.dataset import build_dataset


@pytest.fixture(scope="session")
def dataset(tmp_path_factory) -> tuple[Path, Path]:
    """A freshly built copy of the synthetic dataset, so tests never touch the repo's data folder."""
    data_dir = tmp_path_factory.mktemp("data")
    build_dataset(data_dir)
    return data_dir, data_dir / "claimsleuth.db"
