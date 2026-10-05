"""Import the reviewed Chinook data into this project's local PostgreSQL."""

import argparse
import getpass
import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import psycopg  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402

from database.artifact_io import _save_json  # noqa: E402
from database.connection_settings import (  # noqa: E402
    ConnectionSettingsError,
    protect_password,
)
from database.setup_chinook import (  # noqa: E402
    ChinookSetupError,
    prepare_script,
    setup,
)


def check_local_assets() -> dict:
    cluster = PROJECT_ROOT / ".local" / "postgres" / "data"
    if not (cluster / "PG_VERSION").is_file():
        raise ChinookSetupError(
            "CLUSTER_NOT_INITIALIZED", "Initialize PostgreSQL first."
        )
    if (cluster / "PG_VERSION").read_text(encoding="ascii").strip() != "18":
        raise ChinookSetupError("WRONG_CLUSTER_VERSION", "Expected PostgreSQL 18.")
    _, facts = prepare_script()
    return dict(facts)


def verify_project_server(identity: dict, expected_data_dir: Path) -> None:
    """Do not modify a server belonging to another project on the same port."""
    if (
        identity.get("username") != "postgres"
        or identity.get("database") != "postgres"
        or identity.get("host") not in {"127.0.0.1", "127.0.0.1/32"}
        or identity.get("port") != 5432
        or not 180000 <= int(identity.get("version", 0)) < 190000
    ):
        observed = {
            key: identity.get(key)
            for key in ("username", "database", "host", "port", "version")
        }
        raise ChinookSetupError(
            "WRONG_DATABASE_SERVER",
            "Unexpected server identity: " + json.dumps(observed, ensure_ascii=False),
        )
    data_dir = identity.get("data_directory")
    if (
        not isinstance(data_dir, str)
        or not Path(data_dir).is_absolute()
        or Path(data_dir).resolve() != expected_data_dir.resolve()
    ):
        raise ChinookSetupError(
            "WRONG_DATABASE_DIRECTORY",
            "The server is not using this project's database directory.",
        )


def prepare_local_database(password: str) -> dict:
    parameters: dict[str, Any] = {
        "host": "127.0.0.1",
        "port": 5432,
        "dbname": "postgres",
        "user": "postgres",
        "sslmode": "disable",
    }
    with psycopg.connect(
        **parameters,
        password=password,
        connect_timeout=5,
        autocommit=True,
        row_factory=dict_row,
    ) as connection:
        identity = connection.execute(
            "SELECT current_user AS username, current_database() AS database, "
            "host(inet_server_addr()) AS host, inet_server_port() AS port, "
            "current_setting('server_version_num')::int AS version, "
            "current_setting('data_directory') AS data_directory"
        ).fetchone()
        verify_project_server(
            identity or {}, PROJECT_ROOT / ".local" / "postgres" / "data"
        )

    # The upstream importer reads this encrypted profile; never store plaintext.
    _save_json(
        PROJECT_ROOT / "database" / ".local" / "target_admin_connection.json",
        {**parameters, "password_dpapi": protect_password(password)},
    )
    # Keep the reviewed source_config.json unchanged; it already selects Chinook.
    return dict(setup(prepare_only=True))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="Check local assets; no login or import",
    )
    args = parser.parse_args()
    try:
        facts = check_local_assets()
        if args.check_only:
            result = {"status": "assets_verified", "import_attempted": False, **facts}
        else:
            password = getpass.getpass("PostgreSQL postgres password: ")
            if not password:
                raise ChinookSetupError("EMPTY_PASSWORD", "A password is required.")
            report = prepare_local_database(password)
            result = {"status": "ok", **report}
    except (ChinookSetupError, ConnectionSettingsError) as error:
        result = {"status": "error", "error_code": error.code, "message": str(error)}
    except psycopg.Error:
        result = {
            "status": "error",
            "error_code": "DATABASE_CONNECTION_OR_IMPORT_FAILED",
            "message": "Check that PostgreSQL is running and the password is correct. "
            "Existing database data has not been automatically removed.",
        }
    except (OSError, ValueError):
        result = {
            "status": "error",
            "error_code": "LOCAL_SETUP_FAILED",
            "message": "Check local files and configuration. No database was removed.",
        }
    except (KeyboardInterrupt, EOFError):
        result = {"status": "cancelled"}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] in {"ok", "assets_verified"} else 1


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
