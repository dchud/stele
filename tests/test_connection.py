"""How Databricks connection settings resolve: flag, environment, `.env`."""

from __future__ import annotations

from pathlib import Path

import pytest

from stele import cli
from stele.db import ConfigurationError, DatabricksConfig

CONNECTION_VARS = (
    "DATABRICKS_SERVER_HOSTNAME",
    "DATABRICKS_HOST",
    "DATABRICKS_HTTP_PATH",
    "DATABRICKS_TOKEN",
    "DATABRICKS_CATALOG",
    "DATABRICKS_SCHEMA",
)

COMPLETE = {
    "DATABRICKS_SERVER_HOSTNAME": "adb-1234.1.azuredatabricks.net",
    "DATABRICKS_HTTP_PATH": "/sql/1.0/warehouses/abc123",
    "DATABRICKS_TOKEN": "dapi-secret",
    "DATABRICKS_CATALOG": "federated",
}


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Settings exported in the developer's own shell must not leak in."""
    for name in CONNECTION_VARS:
        monkeypatch.delenv(name, raising=False)


def export(monkeypatch: pytest.MonkeyPatch, **values: str) -> None:
    for name, value in values.items():
        monkeypatch.setenv(name, value)


# --- two names for the hostname -------------------------------------------


def test_databricks_host_is_read_when_the_canonical_name_is_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    export(monkeypatch, **COMPLETE)
    monkeypatch.delenv("DATABRICKS_SERVER_HOSTNAME")
    export(monkeypatch, DATABRICKS_HOST="adb-9999.9.azuredatabricks.net")

    assert DatabricksConfig.from_env().host == "adb-9999.9.azuredatabricks.net"


def test_canonical_name_wins_when_both_are_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    export(
        monkeypatch,
        **COMPLETE,
        DATABRICKS_HOST="adb-9999.9.azuredatabricks.net",
    )

    assert (
        DatabricksConfig.from_env().host
        == COMPLETE["DATABRICKS_SERVER_HOSTNAME"]
    )


def test_hostname_loses_its_scheme_and_trailing_slash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    export(monkeypatch, **COMPLETE)
    monkeypatch.delenv("DATABRICKS_SERVER_HOSTNAME")
    export(
        monkeypatch,
        DATABRICKS_HOST="https://adb-9999.9.azuredatabricks.net/",
    )

    assert DatabricksConfig.from_env().host == "adb-9999.9.azuredatabricks.net"


# --- precedence ------------------------------------------------------------


def test_arguments_override_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    export(monkeypatch, **COMPLETE)

    cfg = DatabricksConfig.from_env(
        catalog="from_flag",
        host="https://flag.example.net/",
        http_path="/sql/1.0/warehouses/flag",
        token="dapi-flag",
    )

    assert cfg.host == "flag.example.net"
    assert cfg.http_path == "/sql/1.0/warehouses/flag"
    assert cfg.token == "dapi-flag"
    assert cfg.catalog == "from_flag"


def test_one_argument_does_not_discard_the_others(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    export(monkeypatch, **COMPLETE)

    cfg = DatabricksConfig.from_env(host="flag.example.net")

    assert cfg.http_path == COMPLETE["DATABRICKS_HTTP_PATH"]
    assert cfg.token == COMPLETE["DATABRICKS_TOKEN"]
    assert cfg.catalog == COMPLETE["DATABRICKS_CATALOG"]


def test_catalog_and_schema_come_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    export(monkeypatch, **COMPLETE, DATABRICKS_SCHEMA="sales")

    cfg = DatabricksConfig.from_env()

    assert cfg.catalog == "federated"
    assert cfg.schema == "sales"


def test_schema_falls_back_to_the_connector_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    export(monkeypatch, **COMPLETE)

    assert DatabricksConfig.from_env().schema == "default"


# --- missing settings ------------------------------------------------------


def test_missing_settings_are_named(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    export(monkeypatch, **COMPLETE)
    monkeypatch.delenv("DATABRICKS_TOKEN")
    monkeypatch.delenv("DATABRICKS_CATALOG")

    with pytest.raises(ConfigurationError) as caught:
        DatabricksConfig.from_env()

    assert caught.value.missing == ("token", "catalog")


def test_blank_values_count_as_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    export(monkeypatch, **COMPLETE)
    export(monkeypatch, DATABRICKS_SERVER_HOSTNAME="   ")

    with pytest.raises(ConfigurationError) as caught:
        DatabricksConfig.from_env()

    assert caught.value.missing == ("host",)


def test_cli_reports_missing_settings_without_a_traceback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = cli.build_parser().parse_args(["introspect", "--schemas", "dbo"])

    with pytest.raises(SystemExit) as caught:
        cli._config(args)

    message = str(caught.value)
    assert "host" in message
    assert "DATABRICKS_SERVER_HOSTNAME" in message
    assert "DATABRICKS_HOST" in message
    assert ".env" in message
    assert "--host" in message


def test_cli_resolves_the_catalog_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    export(monkeypatch, **COMPLETE)
    args = cli.build_parser().parse_args(["introspect", "--schemas", "dbo"])

    assert cli._config(args).catalog == "federated"


def test_cli_catalog_flag_overrides_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    export(monkeypatch, **COMPLETE)
    args = cli.build_parser().parse_args(
        ["introspect", "--schemas", "dbo", "--catalog", "from_flag"]
    )

    assert cli._config(args).catalog == "from_flag"


# --- the .env file ---------------------------------------------------------


def test_env_file_supplies_unset_variables(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / ".env").write_text(
        "DATABRICKS_TOKEN=dapi-from-file\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)

    assert cli._load_env_file() == str(tmp_path / ".env")
    export(monkeypatch, **COMPLETE)
    monkeypatch.delenv("DATABRICKS_TOKEN")
    cli._load_env_file()

    assert DatabricksConfig.from_env().token == "dapi-from-file"


def test_exported_variable_beats_the_env_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / ".env").write_text(
        "DATABRICKS_TOKEN=dapi-from-file\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    export(monkeypatch, **COMPLETE)

    cli._load_env_file()

    assert DatabricksConfig.from_env().token == COMPLETE["DATABRICKS_TOKEN"]


def test_env_file_is_found_from_a_subdirectory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / ".env").write_text(
        "DATABRICKS_CATALOG=from_file\n", encoding="utf-8"
    )
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)

    assert cli._load_env_file() == str(tmp_path / ".env")


def test_no_env_file_is_not_an_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)

    assert cli._load_env_file() is None
