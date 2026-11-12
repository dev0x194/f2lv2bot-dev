import os
import subprocess
import yaml
import pytest


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEV_COMPOSE_PATH = os.path.join(REPO_ROOT, "docker-compose.dev.yml")
PROD_COMPOSE_PATH = os.path.join(REPO_ROOT, "docker-compose.yml")
DEV_ENV_EXAMPLE_PATH = os.path.join(REPO_ROOT, "dev.env.example")
DEV_ENV_PATH = os.path.join(REPO_ROOT, "dev.env")
GITIGNORE_PATH = os.path.join(REPO_ROOT, ".gitignore")


def test_dev_compose_exists_and_is_valid_yaml():
    assert os.path.isfile(DEV_COMPOSE_PATH), "docker-compose.dev.yml must exist"
    with open(DEV_COMPOSE_PATH, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    assert isinstance(data, dict)
    assert data.get("name") == "filetolinkv2bot-dev"
    assert "services" in data
    assert "filestreambot-dev" in data["services"]
    assert "networks" in data
    assert "dev-network" in data["networks"]
    assert "volumes" in data
    assert "filestream-cache-dev" in data["volumes"]
    assert "filestream-sessions-dev" in data["volumes"]


def test_dev_compose_resource_isolation_against_production():
    with open(PROD_COMPOSE_PATH, "r", encoding="utf-8") as f:
        prod_data = yaml.safe_load(f)

    with open(DEV_COMPOSE_PATH, "r", encoding="utf-8") as f:
        dev_data = yaml.safe_load(f)

    # 1. Project name isolation
    assert dev_data.get("name") != prod_data.get("name")

    # 2. Service name isolation
    prod_services = set(prod_data.get("services", {}).keys())
    dev_services = set(dev_data.get("services", {}).keys())
    assert prod_services.isdisjoint(dev_services), f"Colliding services: {prod_services & dev_services}"

    # 3. Container name isolation
    prod_container_names = {
        s.get("container_name") for s in prod_data.get("services", {}).values() if "container_name" in s
    }
    dev_container_names = {
        s.get("container_name") for s in dev_data.get("services", {}).values() if "container_name" in s
    }
    assert prod_container_names.isdisjoint(dev_container_names)
    assert "filetolinkv2bot-filestreambot-1" not in dev_container_names
    assert "filetolinkv2bot-traefik-1" not in dev_container_names

    # 4. Network isolation
    dev_net_def = dev_data.get("networks", {}).get("dev-network", {})
    assert "filestreambot-dev-network" in str(dev_net_def)
    assert "filestreambot-proxy" not in str(dev_net_def)

    # 5. Volume isolation
    prod_vols = set(prod_data.get("volumes", {}).keys())
    dev_vols = set(dev_data.get("volumes", {}).keys())
    assert prod_vols.isdisjoint(dev_vols), f"Colliding volumes: {prod_vols & dev_vols}"
    assert "filestream-cache" not in dev_vols
    assert "letsencrypt" not in dev_vols

    # 6. Traefik label isolation (dev should not hijack production traefik router)
    filestream_dev_service = dev_data["services"]["filestreambot-dev"]
    labels = filestream_dev_service.get("labels", [])
    for label in labels:
        assert "traefik.http.routers.filestreambot.rule" not in str(label)


def test_dev_env_example_and_gitignore_safety():
    assert os.path.isfile(DEV_ENV_EXAMPLE_PATH), "dev.env.example must exist"
    with open(DEV_ENV_EXAMPLE_PATH, "r", encoding="utf-8") as f:
        content = f.read()

    # Confirm critical placeholders are present
    assert "BOT_TOKEN=REPLACE_WITH_DEV_BOT_TOKEN" in content
    assert "API_ID=REPLACE_WITH_DEV_API_ID" in content
    assert "API_HASH=REPLACE_WITH_DEV_API_HASH" in content
    assert "SESSION_NAME=FileStreamDev" in content
    assert "DATABASE_URL=mongodb" in content
    assert "DEV_HOST_PORT=8081" in content or "DEV_HOST_PORT=18080" in content

    # Check .gitignore ignores dev.env
    with open(GITIGNORE_PATH, "r", encoding="utf-8") as f:
        gi = f.read()

    assert "dev.env" in gi
    assert "!dev.env.example" in gi
    assert "streambot_dev.log" in gi


def test_docker_compose_config_validation():
    result = subprocess.run(
        ["docker", "compose", "-f", "docker-compose.dev.yml", "--env-file", "dev.env", "config"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"docker compose config failed: {result.stderr}"
    assert "filetolinkv2bot-dev" in result.stdout
    assert "filestreambot-dev" in result.stdout
    assert "8081" in result.stdout or "18080" in result.stdout


def test_runtime_configuration_respects_session_and_env():
    from FileStream.config import Telegram
    import FileStream.bot as bot_module

    # Verify that Telegram.SESSION_NAME is defined and used by FileStream Client
    assert hasattr(Telegram, "SESSION_NAME")
    assert hasattr(bot_module, "FileStream")
    assert hasattr(bot_module, "SESSION_DIR")
