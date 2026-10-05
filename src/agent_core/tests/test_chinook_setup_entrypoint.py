"""The setup entrypoint must not provision an unrelated local server."""

import importlib.util
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location(
    "project_chinook_setup", ROOT / "scripts" / "prepare_chinook.py"
)
assert SPEC is not None and SPEC.loader is not None
entrypoint = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(entrypoint)


def identity(data_dir: Path) -> dict:
    return {
        "username": "postgres",
        "database": "postgres",
        "host": "127.0.0.1",
        "port": 5432,
        "version": 180006,
        "data_directory": str(data_dir),
    }


@pytest.mark.parametrize(
    "changes",
    [
        {"data_directory": str(ROOT / "unrelated-cluster")},
        {"username": "other_admin"},
        {"host": "192.168.1.10"},
        {"host": "192.168.1.10/32"},
        {"host": "127.0.0.1/0"},
        {"version": 170011},
    ],
)
def test_wrong_server_cannot_save_credentials_or_import(changes: dict) -> None:
    server_identity = identity(ROOT / ".local" / "postgres" / "data") | changes
    connection = MagicMock()
    connection.__enter__.return_value = connection
    connection.execute.return_value.fetchone.return_value = server_identity
    with (
        patch.object(entrypoint.psycopg, "connect", return_value=connection),
        patch.object(entrypoint, "_save_json") as save,
        patch.object(entrypoint, "setup") as setup,
    ):
        with pytest.raises(entrypoint.ChinookSetupError):
            entrypoint.prepare_local_database("test-only-password")
        save.assert_not_called()
        setup.assert_not_called()


@pytest.mark.parametrize("server_host", ["127.0.0.1", "127.0.0.1/32"])
def test_valid_server_saves_only_encrypted_password_then_prepares(
    server_host: str,
) -> None:
    connection = MagicMock()
    connection.__enter__.return_value = connection
    connection.execute.return_value.fetchone.return_value = identity(
        ROOT / ".local" / "postgres" / "data"
    ) | {"host": server_host}
    with (
        patch.object(entrypoint.psycopg, "connect", return_value=connection),
        patch.object(
            entrypoint, "protect_password", return_value="encrypted-test-value"
        ),
        patch.object(entrypoint, "_save_json") as save,
        patch.object(
            entrypoint, "setup", return_value={"database": "chinook"}
        ) as setup,
    ):
        entrypoint.prepare_local_database("test-only-password")
        saved_profile = save.call_args.args[1]
        assert "password" not in saved_profile
        assert saved_profile["password_dpapi"] == "encrypted-test-value"
        setup.assert_called_once_with(prepare_only=True)


def test_substituted_path_resolves_to_the_project_cluster() -> None:
    if not Path("P:/").exists() or Path("P:/").resolve() != ROOT.resolve():
        pytest.skip("The user-created project drive mapping is not present.")
    entrypoint.verify_project_server(
        identity(Path("P:/.local/postgres/data")),
        ROOT / ".local" / "postgres" / "data",
    )
