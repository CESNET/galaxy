import os
import socket

import pytest
import yaml

from galaxy.files.models import (
    FileSourcePluginsConfig,
    FilesSourceRuntimeContext,
    UserData,
)
from galaxy.files.plugins import FileSourcePluginLoader
from galaxy.files.sources.irods import IrodsFilesSource
from ._util import (
    assert_realizes_contains,
    configured_file_sources,
    write_from,
)

try:
    from irods.session import iRODSSession
except ImportError:
    iRODSSession = None


ROUNDTRIP_TEST_FILENAME = "numerical_sort_and_write_back_to_irods_v2.tab"


class _FakeSession:
    init_kwargs = None

    def __init__(self, **kwargs):
        type(self).init_kwargs = kwargs
        self.connection_timeout = None
        self.default_resource = None


class _FakeIrodsFs:
    def __init__(self, session, root=None):
        self.session = session
        self.root = root


def _get_setting(irods_config: dict, env_name: str, key: str, cast=None):
    raw = os.environ.get(env_name)
    if raw is None:
        raw = irods_config.get(key)
    if raw is None:
        pytest.skip(f"Missing iRODS setting '{key}' in file_sources_conf.yml or env {env_name}.")
    return cast(raw) if cast else raw


def _file_sources_config(irods_config: dict) -> dict:
    return {
        "host": _get_setting(irods_config, "GALAXY_TEST_IRODS_HOST", "host"),
        "port": _get_setting(irods_config, "GALAXY_TEST_IRODS_PORT", "port", int),
        "username": _get_setting(irods_config, "GALAXY_TEST_IRODS_USER", "username"),
        "password": _get_setting(irods_config, "GALAXY_TEST_IRODS_PASSWORD", "password"),
        "zone": _get_setting(irods_config, "GALAXY_TEST_IRODS_ZONE", "zone"),
        "root": _get_setting(irods_config, "GALAXY_TEST_IRODS_ROOT", "root"),
        "timeout": _get_setting(irods_config, "GALAXY_TEST_IRODS_TIMEOUT", "timeout", int),
        "refresh_time": _get_setting(irods_config, "GALAXY_TEST_IRODS_REFRESH_TIME", "refresh_time", int),
    }


def _irods_live_settings() -> dict:
    config_path = os.environ.get(
        "GALAXY_TEST_FILE_SOURCES_CONFIG",
        os.path.abspath(
            os.path.join(os.path.dirname(__file__), os.pardir, os.pardir, os.pardir, "config", "file_sources_conf.yml")
        ),
    )
    if not os.path.exists(config_path):
        pytest.skip(f"No file sources config at {config_path}; set file_sources_conf.yml to enable iRODS live tests.")

    with open(config_path, "rb") as handle:
        configs = yaml.safe_load(handle) or []

    irods_id = os.environ.get("GALAXY_TEST_IRODS_SOURCE_ID")
    irods_configs = [c for c in configs if isinstance(c, dict) and c.get("type") == "irods"]
    if irods_id:
        irods_configs = [c for c in irods_configs if c.get("id") == irods_id]

    if not irods_configs:
        pytest.skip("No iRODS file source found in file_sources_conf.yml; configure one to enable live tests.")

    irods_config = irods_configs[0]

    return _file_sources_config(irods_config)


def _skip_if_irods_unreachable(host: str, port: int):
    try:
        with socket.create_connection((host, port), timeout=1):
            return
    except OSError:
        pytest.skip(
            f"No reachable iRODS service at {host}:{port}. "
            "Start your local iRODS Docker stack or override GALAXY_TEST_IRODS_* settings."
        )


def _live_file_source_config(settings: dict, writable: bool = False) -> list[dict]:
    return [
        {
            "type": "irods",
            "id": "test1",
            "label": "iRODS Live Test",
            "doc": "Live iRODS connectivity smoke test",
            "host": settings["host"],
            "port": settings["port"],
            "username": settings["username"],
            "password": settings["password"],
            "zone": settings["zone"],
            "root": settings["root"],
            "timeout": settings["timeout"],
            "refresh_time": settings["refresh_time"],
            "writable": writable,
        }
    ]


def _cleanup_live_test_artifacts(settings: dict):
    root = settings["root"].rstrip("/")
    logical_path = f"{root}/{ROUNDTRIP_TEST_FILENAME}"

    session = iRODSSession(
        host=settings["host"],
        port=settings["port"],
        user=settings["username"],
        password=settings["password"],
        zone=settings["zone"],
        refresh_time=settings["refresh_time"],
    )
    session.connection_timeout = settings["timeout"]

    try:
        if session.data_objects.exists(logical_path):
            session.data_objects.unlink(logical_path)
    finally:
        session.cleanup()


def test_irods_plugin_registered():
    plugin_loader = FileSourcePluginLoader()
    plugin_class = plugin_loader.get_plugin_type_class("irods")
    assert plugin_class is IrodsFilesSource


def test_irods_open_fs_builds_session(monkeypatch):
    monkeypatch.setattr("galaxy.files.sources.irods.iRODSSession", _FakeSession)
    monkeypatch.setattr("galaxy.files.sources.irods.iRODSFS", _FakeIrodsFs)
    monkeypatch.setattr(IrodsFilesSource, "required_module", _FakeIrodsFs)

    file_source = IrodsFilesSource(
        IrodsFilesSource.build_template_config(
            type="irods",
            id="test_irods",
            file_sources_config=FileSourcePluginsConfig(),
            host="irods.example.org",
            port=1247,
            username="rods",
            password="secret",
            zone="tempZone",
            root="/tempZone/home/rods",
            timeout=42,
            refresh_time=120,
            resource="demoResc",
            writable=True,
        )
    )

    resolved_config = file_source._evaluate_template_config(UserData())
    context = FilesSourceRuntimeContext(user_data=UserData(), config=resolved_config)

    fs = file_source._open_fs(context)
    init_kwargs = _FakeSession.init_kwargs

    assert isinstance(fs, _FakeIrodsFs)
    assert fs.root == "/tempZone/home/rods"
    assert init_kwargs is not None
    assert init_kwargs["host"] == "irods.example.org"
    assert init_kwargs["port"] == 1247
    assert init_kwargs["user"] == "rods"
    assert init_kwargs["password"] == "secret"
    assert init_kwargs["zone"] == "tempZone"
    assert init_kwargs["refresh_time"] == 120
    assert fs.session.connection_timeout == 42
    assert fs.session.default_resource == "demoResc"


def test_irods_live_touch():
    settings = _irods_live_settings()
    _skip_if_irods_unreachable(settings["host"], settings["port"])
    _cleanup_live_test_artifacts(settings)

    file_sources = configured_file_sources(_live_file_source_config(settings, writable=False))
    file_source_pair = file_sources.get_file_source_path("gxfiles://test1")

    assert file_source_pair.path == "/"
    entries, count = file_source_pair.file_source.list("/", recursive=False)
    assert isinstance(entries, list)
    assert count >= 0
    _cleanup_live_test_artifacts(settings)


def test_irods_live_recursive_list():
    settings = _irods_live_settings()
    _skip_if_irods_unreachable(settings["host"], settings["port"])
    _cleanup_live_test_artifacts(settings)

    file_sources = configured_file_sources(_live_file_source_config(settings, writable=False))
    file_source_pair = file_sources.get_file_source_path("gxfiles://test1")

    entries, count = file_source_pair.file_source.list("/", recursive=True)
    assert isinstance(entries, list)
    assert count >= 0
    _cleanup_live_test_artifacts(settings)


def test_irods_live_write_and_read_roundtrip():
    settings = _irods_live_settings()
    _skip_if_irods_unreachable(settings["host"], settings["port"])
    _cleanup_live_test_artifacts(settings)

    test_contents = "1\t2\t999\n666\t6\t555\n3\t4\t5\n"
    target_uri = f"gxfiles://test1/{ROUNDTRIP_TEST_FILENAME}"

    file_sources = configured_file_sources(_live_file_source_config(settings, writable=True))
    _ = write_from(file_sources, target_uri, test_contents)
    assert_realizes_contains(file_sources, target_uri, test_contents)

    _cleanup_live_test_artifacts(settings)
