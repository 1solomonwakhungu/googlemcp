"""Validates the Official MCP Registry server.json manifest and README pairing."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SERVER_JSON = ROOT / "server.json"
PYPROJECT_TOML = ROOT / "pyproject.toml"
README_MD = ROOT / "README.md"

EXPECTED_SCHEMA = "https://static.modelcontextprotocol.io/schemas/2025-12-11/server.schema.json"
EXPECTED_NAME = "io.github.1solomonwakhungu/googlemcp"
EXPECTED_MCP_MARKER = f"<!-- mcp-name: {EXPECTED_NAME} -->"
MAX_DESCRIPTION_LEN = 100


def _load_manifest() -> dict[str, Any]:
    data = json.loads(SERVER_JSON.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def _project_version() -> str:
    pyproject = tomllib.loads(PYPROJECT_TOML.read_text(encoding="utf-8"))
    version = pyproject["project"]["version"]
    assert isinstance(version, str)
    return version


def test_server_manifest_core_fields() -> None:
    manifest = _load_manifest()
    project_version = _project_version()

    assert manifest["$schema"] == EXPECTED_SCHEMA
    assert manifest["name"] == EXPECTED_NAME
    assert manifest["title"] == "GoogleMCP"
    assert 1 <= len(manifest["description"]) <= MAX_DESCRIPTION_LEN
    assert manifest["version"] == project_version
    assert manifest["repository"]["source"] == "github"
    assert manifest["repository"]["url"] == "https://github.com/1solomonwakhungu/googlemcp"


def test_server_manifest_pypi_package() -> None:
    manifest = _load_manifest()
    packages = manifest["packages"]
    assert isinstance(packages, list)
    assert len(packages) == 1

    pkg = packages[0]
    assert pkg["registryType"] == "pypi"
    assert pkg["registryBaseUrl"] == "https://pypi.org"
    assert pkg["identifier"] == "googlemcp"
    assert pkg["version"] == manifest["version"]
    assert pkg["runtimeHint"] == "uvx"
    assert pkg["transport"]["type"] == "stdio"

    args = pkg["packageArguments"]
    assert args == [
        {
            "type": "positional",
            "value": "serve",
            "description": "Run the local stdio MCP server.",
        }
    ]

    env_names = [item["name"] for item in pkg["environmentVariables"]]
    assert env_names == [
        "GOOGLEMCP_PRODUCTS",
        "GOOGLEMCP_WRITE_PRODUCTS",
        "GOOGLEMCP_ALLOW_DESTRUCTIVE",
        "GOOGLEMCP_SECRET_BACKEND",
    ]
    for item in pkg["environmentVariables"]:
        assert item["isRequired"] is False
        assert item["isSecret"] is False


def test_readme_contains_mcp_name_marker() -> None:
    readme_text = README_MD.read_text(encoding="utf-8")
    assert EXPECTED_MCP_MARKER in readme_text
    assert readme_text.startswith(EXPECTED_MCP_MARKER)


def test_server_manifest_has_no_em_dashes() -> None:
    em_dash = chr(0x2014)
    assert em_dash not in SERVER_JSON.read_text(encoding="utf-8")
