"""A nodepack folder with neither .git nor .tracking is an invalid installation.

Such a folder comes from an install that broke midway, or from a download that somebody
unpacked into custom_nodes by hand: the Manager can neither update it nor tell its version.
The real Manager scans a temporary custom_nodes; the registry, the node list of the channel,
the archive download and the git clone are stand-ins.
"""
import ast
import asyncio
import importlib.util
import logging
import os
import re
import subprocess
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from _install_flags_testutil import GIT_LOCATION, import_reader
from js_lift import NODE, JsSource, line_containing, parse, run_node, slice_braced
from legacy_html_testutil import PACKAGE

REGISTRY = [
    {'id': 'comfyui-foo', 'name': 'Foo', 'repository': 'https://github.com/example/ComfyUI-Foo',
     'publisher': {'name': 'example'}, 'description': '', 'latest_version': {'version': '1.0.1'}},
    {'id': 'comfyui-baz', 'name': 'Baz', 'repository': 'https://github.com/example/comfyui-baz',
     'publisher': {'name': 'example'}, 'description': '', 'latest_version': {'version': '2.0.0'}},
]
FOO_URL = 'https://github.com/example/ComfyUI-Foo'
BAR_URL = 'https://github.com/example/ComfyUI-Bar'
NODE_LIST = [
    {'author': 'example', 'title': 'ComfyUI-Foo', 'reference': FOO_URL, 'files': [FOO_URL],
     'install_type': 'git-clone', 'description': ''},
    {'author': 'example', 'title': 'ComfyUI-Bar', 'reference': BAR_URL, 'files': [BAR_URL],
     'install_type': 'git-clone', 'description': ''},
]
INVALID = {'ver': 'unknown', 'aux_id': None, 'invalid_installation': True}
# what a user reads about an invalid installation, wherever it is shown
GUIDANCE = ('This nodepack was not installed through ComfyUI-Manager, or its installation is damaged, so the Manager '
            'cannot update it. Reinstalling deletes its folder and installs the nodepack again.')


def pack(path, project_name=None, version='1.0.1', tracking=False, git_url=None):
    """A nodepack folder: its files, and what marks how it was installed."""
    path.mkdir(parents=True)
    (path / '__init__.py').write_text('NODE_CLASS_MAPPINGS = {}\n')
    if project_name is not None:
        (path / 'pyproject.toml').write_text(f'[project]\nname = "{project_name}"\nversion = "{version}"\n')
    if tracking:
        (path / '.tracking').write_text('__init__.py\npyproject.toml\n')
    if git_url is not None:
        (path / '.git').mkdir()
        (path / '.git/HEAD').write_text('0123456789abcdef0123456789abcdef01234567\n')
        (path / '.git/config').write_text(f'[remote "origin"]\n\turl = {git_url}\n')
    return path


@pytest.fixture(params=['glob', 'legacy'])
def core(request):
    return import_reader(request.param)


@pytest.fixture
def custom_nodes(core, monkeypatch, tmp_path):
    custom_nodes = tmp_path / 'custom_nodes'
    custom_nodes.mkdir()
    monkeypatch.setattr(sys.modules['folder_paths'], 'get_folder_paths', lambda name: [str(custom_nodes)], raising=False)
    monkeypatch.setattr(core, 'get_default_custom_nodes_path', lambda: str(custom_nodes))
    config = {'network_mode': 'public', 'channel_url': core.DEFAULT_CHANNEL, 'http_channel_enabled': False,
              'allow_flagged_nodepack_install': False}
    monkeypatch.setattr(core, 'get_config', lambda: config)

    async def get_cnr_data(*args, **kwargs):
        return REGISTRY

    async def get_data_by_mode(mode, filename, channel_url=None):
        return {'custom_nodes': [dict(item) for item in NODE_LIST]}

    monkeypatch.setattr(core.cnr_utils, 'get_cnr_data', get_cnr_data)
    monkeypatch.setattr(core, 'get_data_by_mode', get_data_by_mode)
    manager = core.UnifiedManager()
    manager.execute_install_script = Mock(return_value=True)
    monkeypatch.setattr(core, 'unified_manager', manager)
    return custom_nodes


def git_pack(path, origin):
    """A git clone of a local repository `origin`, one commit behind it."""
    env = {name: value for name, value in os.environ.items() if name not in GIT_LOCATION}

    def git(*args, cwd, date='2024-01-01T00:00:00'):
        subprocess.run(['git', '-c', 'user.name=test', '-c', 'user.email=test@example.invalid', *args], cwd=cwd,
                       env=dict(env, GIT_AUTHOR_DATE=date, GIT_COMMITTER_DATE=date), check=True, capture_output=True)

    origin.mkdir(parents=True)
    git('init', '-q', cwd=origin)
    for version in (1, 2):
        (origin / '__init__.py').write_text(f'VERSION = {version}\n')
        git('add', '.', cwd=origin)
        # an update is a remote commit that is newer
        git('commit', '-q', '-m', f'version {version}', cwd=origin, date=f'2024-01-0{version}T00:00:00')
    git('clone', '-q', str(origin), str(path), cwd=origin.parent)
    git('reset', '-q', '--hard', 'HEAD~1', cwd=path)
    return path


def reload(core):
    asyncio.run(core.unified_manager.reload('cache'))
    return core.unified_manager


def flagged_rows(core):
    """The rows of the node list that carry the flag, with their state."""
    rows = asyncio.run(core.get_unified_total_nodes(core.DEFAULT_CHANNEL, 'cache'))
    return {key: row['state'] for key, row in rows.items() if row.get('invalid-installation')}


def registry_api(monkeypatch, core, status='NodeVersionStatusActive'):
    """The registry answers for comfyui-foo@1.0.1; the download is an archive of that version."""
    monkeypatch.setattr(core.cnr_utils.requests, 'get', Mock(return_value=SimpleNamespace(
        status_code=200, json=lambda: {
            'id': 'version-id', 'node_id': 'comfyui-foo', 'version': '1.0.1', 'status': status,
            'downloadUrl': 'https://example.invalid/comfyui-foo.zip'})))
    monkeypatch.setattr(core.manager_funcs, 'is_flagged_install_allowed', lambda node_id: False)

    def download(url, directory, name):
        with zipfile.ZipFile(Path(directory) / name, 'w') as archive:
            archive.writestr('__init__.py', 'NODE_CLASS_MAPPINGS = {}  # 1.0.1\n')
            archive.writestr('pyproject.toml', '[project]\nname = "comfyui-foo"\nversion = "1.0.1"\n')

    monkeypatch.setattr(core.manager_downloader, 'download_url', download)


def git_clone_stand_in(monkeypatch, core):
    """A clone writes the repository the url names, without the network."""
    def clone(url, path, **kwargs):
        pack(Path(path), git_url=url)
        return SimpleNamespace(clear_cache=lambda: None, close=lambda: None, git=SimpleNamespace(clear_cache=lambda: None))

    if hasattr(core, 'clone_repo'):
        monkeypatch.setattr(core, 'clone_repo', clone)
    else:
        monkeypatch.setattr(core.git.Repo, 'clone_from', staticmethod(clone))


# --- what is an invalid installation, and which pack it is

@pytest.mark.parametrize('folder, project_name, cnr_id, row', [
    ('comfyui-foo', 'comfyui-foo', 'comfyui-foo', 'comfyui-foo'),           # a registry install whose .tracking is gone
    ('ComfyUI-Foo-main', 'ComfyUI-Foo', 'comfyui-foo', 'comfyui-foo'),      # a GitHub zip unpacked by hand
    ('comfyui-baz', 'comfyui-foo', 'comfyui-foo', 'comfyui-foo'),           # rule 1 before rule 2
    ('comfyui-baz', 'not-in-the-registry', 'comfyui-baz', 'comfyui-baz'),   # rule 2
    ('ComfyUI-Baz', None, 'comfyui-baz', 'comfyui-baz'),                    # rule 2, ids compare case-insensitively
    ('ComfyUI-Bar', None, '', 'ComfyUI-Bar'),                               # rule 3, a non-registry row by its key
    ('Something-Else', None, '', None),                                     # rule 3, no row has that key
], ids=['no-tracking', 'github-zip', 'project-name-first', 'folder-name', 'folder-name-case', 'non-registry', 'no-row'])
def test_the_pack_of_an_invalid_installation(core, custom_nodes, folder, project_name, cnr_id, row):
    pack(custom_nodes / folder, project_name)
    reload(core)

    assert core.get_installed_node_packs() == {folder: {**INVALID, 'cnr_id': cnr_id, 'enabled': True}}
    # ComfyUI loads the folder: the row is installed, whether it is a registry row or not
    assert flagged_rows(core) == ({row: 'enabled'} if row else {})


@pytest.mark.parametrize('folder, project_name, before, after', [
    ('comfyui-foo', 'comfyui-foo', 'comfyui-foo', 'comfyui-foo'),                     # a registry install whose .tracking is gone
    ('ComfyUI-Foo-main', 'ComfyUI-Foo', 'comfyui-foo', 'comfyui-foo'),                # a GitHub zip, the name as read_cnr_info has it
    ('comfyui-baz', 'not-in-the-registry', 'not-in-the-registry', 'comfyui-baz'),     # rule 2 needs the list
    ('Something-Else', 'something-else', 'something-else', 'something-else'),         # a name the registry does not have
    ('comfyui-baz', None, '', 'comfyui-baz'),                                         # no pyproject.toml: the folder name needs the list
    ('ComfyUI-Bar', None, '', ''),
], ids=['no-tracking', 'github-zip', 'folder-name', 'not-in-the-registry', 'no-pyproject', 'non-registry'])
def test_the_installed_map_tells_the_pack_before_the_registry_list_is_loaded(core, custom_nodes, folder, project_name,
                                                                            before, after):
    """The page may ask for the installed map while the registry list is still loading. A pack that its own
    pyproject.toml names is told without the list, as the entry of a registry install is."""
    pack(custom_nodes / folder, project_name)

    assert not core.unified_manager.cnr_map
    assert core.get_installed_node_packs() == {folder: {**INVALID, 'cnr_id': before, 'enabled': True}}
    reload(core)
    assert core.get_installed_node_packs() == {folder: {**INVALID, 'cnr_id': after, 'enabled': True}}


@pytest.mark.parametrize('folder, project_name, cnr_id, row', [
    ('ComfyUI-Bar', None, '', 'ComfyUI-Bar'), ('ComfyUI-Foo-main', 'comfyui-foo', 'comfyui-foo', 'comfyui-foo'),
], ids=['non-registry', 'registry'])
def test_a_disabled_invalid_installation(core, custom_nodes, folder, project_name, cnr_id, row):
    pack(custom_nodes / '.disabled' / folder, project_name)
    reload(core)

    assert core.get_installed_node_packs() == {folder: {**INVALID, 'cnr_id': cnr_id, 'enabled': False}}
    assert flagged_rows(core) == {row: 'disabled'}


def test_what_is_never_an_invalid_installation(core, custom_nodes, capsys):
    pack(custom_nodes / 'comfyui-foo', 'comfyui-foo', tracking=True)
    pack(custom_nodes / 'ComfyUI-Bar', git_url=BAR_URL)
    pack(custom_nodes / 'not-in-the-registry', 'not-in-the-registry', version='3.0.0', tracking=True)
    pack(custom_nodes / 'tracking-without-pyproject', tracking=True)
    pack(custom_nodes / 'comfyui-manager', 'comfyui-manager')
    (custom_nodes / '__pycache__').mkdir()
    (custom_nodes / '.disabled/.trash').mkdir(parents=True)
    (custom_nodes / 'single_file_node.py').write_text('')
    reload(core)

    assert core.get_installed_node_packs() == {
        # what these entries were before invalid installations were recognised
        'comfyui-foo': {'ver': '1.0.1', 'cnr_id': 'comfyui-foo', 'aux_id': None, 'enabled': True},
        'ComfyUI-Bar': {'ver': '0123456789abcdef0123456789abcdef01234567', 'cnr_id': '',
                        'aux_id': 'example/ComfyUI-Bar', 'enabled': True},
        'not-in-the-registry': {'ver': '3.0.0', 'cnr_id': 'not-in-the-registry', 'aux_id': None, 'enabled': True},
    }
    assert flagged_rows(core) == {}
    core.check_invalid_nodes()
    assert 'Nodes requiring reinstallation' not in capsys.readouterr().out


def test_a_reload_forgets_a_folder_that_is_gone(core, custom_nodes):
    folder = pack(custom_nodes / 'comfyui-foo', 'comfyui-foo')
    assert flagged_rows(core) == {'comfyui-foo': 'enabled'}

    (folder / '.tracking').write_text('__init__.py\n')

    assert flagged_rows(core) == {}


def test_disable_and_enable_by_the_pack_id(core, custom_nodes):
    pack(custom_nodes / 'ComfyUI-Foo-main', 'comfyui-foo')
    manager = reload(core)

    assert manager.unified_disable('comfyui-foo', True).result
    assert core.get_installed_node_packs() == {'comfyui-foo': {**INVALID, 'cnr_id': 'comfyui-foo', 'enabled': False}}

    assert manager.unified_enable('comfyui-foo', 'unknown').result
    assert core.get_installed_node_packs() == {'comfyui-foo': {**INVALID, 'cnr_id': 'comfyui-foo', 'enabled': True}}


def test_the_notice_lists_the_invalid_folders_that_a_reload_found(core, custom_nodes, capsys):
    folder = pack(custom_nodes / 'ComfyUI-Bar')
    reload(core)

    core.check_invalid_nodes()

    out = capsys.readouterr().out
    assert 'Nodes requiring reinstallation have been detected' in out
    assert 'not installed through ComfyUI-Manager, or their installation is damaged' in out
    assert str(folder) in out


# --- update

def test_fetching_updates_leaves_an_invalid_installation_alone(core, custom_nodes, capsys):
    pack(custom_nodes / 'ComfyUI-Bar')
    manager = reload(core)

    assert manager.fetch_or_pull_git_repo(is_pull=True) == {'updated': [], 'failed': []}
    assert 'Traceback' not in capsys.readouterr().err


def test_fetching_updates_reaches_a_git_pack_whose_id_a_stray_invalid_folder_shares(core, custom_nodes, tmp_path):
    git_pack(custom_nodes / 'bar-clone', tmp_path / 'origins/ComfyUI-Bar')
    pack(custom_nodes / '.disabled/ComfyUI-Bar')
    manager = reload(core)

    assert manager.fetch_or_pull_git_repo(is_pull=False) == {'updated': ['ComfyUI-Bar@unknown'], 'failed': []}


def update_all(monkeypatch, core):
    """Run Update All of the server of `core`; return the packs it queued an update for."""
    models = data_models(monkeypatch)
    queued = []
    server = core.__name__.split('.')[-2]

    async def save_snapshot_with_postfix(postfix):
        pass

    monkeypatch.setattr(core, 'save_snapshot_with_postfix', save_snapshot_with_postfix, raising=False)
    namespace = {name: getattr(models, name) for name in dir(models) if not name.startswith('_')}
    namespace.update(
        core=core, logging=logging, os=os,
        web=SimpleNamespace(Response=lambda status: status),
        security_utils=SimpleNamespace(is_allowed_security_level=lambda level: True),
        is_allowed_security_level=lambda level: True,
        task_queue=SimpleNamespace(put=lambda task: queued.append(task.params.node_name)),
        temp_queue_batch=Mock(append=lambda item: queued.append(item[1][1])),
        task_worker_lock=Mock(__enter__=Mock(), __exit__=Mock(return_value=False)), task_worker_thread=None,
    )
    path = PACKAGE / server / 'manager_server.py'
    nodes = [node for node in ast.parse(path.read_text(encoding='utf-8')).body
             if isinstance(node, ast.AsyncFunctionDef) and node.name == '_update_all']
    exec(compile(ast.Module(nodes, []), str(path), 'exec'), namespace)
    if server == 'glob':
        params = models.UpdateAllQueryParams(client_id='client', ui_id='ui', mode='cache')
    else:
        params = {'mode': 'cache'}
    assert asyncio.run(namespace['_update_all'](params)) == 200
    return queued


def test_update_all_queues_no_update_for_an_invalid_installation(core, custom_nodes, monkeypatch):
    pack(custom_nodes / 'ComfyUI-Bar')
    pack(custom_nodes / 'ComfyUI-Foo-main', 'comfyui-foo')
    pack(custom_nodes / 'comfyui-baz', 'comfyui-baz', version='2.0.0', tracking=True)
    pack(custom_nodes / 'ComfyUI-Qux', git_url='https://github.com/example/ComfyUI-Qux')

    assert sorted(update_all(monkeypatch, core)) == ['ComfyUI-Qux', 'comfyui-baz']


def test_update_all_updates_a_git_pack_whose_id_a_stray_invalid_folder_shares(core, custom_nodes, monkeypatch):
    pack(custom_nodes / 'bar-clone', git_url=BAR_URL)
    pack(custom_nodes / '.disabled/ComfyUI-Bar')

    assert update_all(monkeypatch, core) == ['ComfyUI-Bar']


def test_updating_an_invalid_installation_says_why_it_fails(core, custom_nodes):
    folder = pack(custom_nodes / 'ComfyUI-Bar')
    manager = reload(core)

    result = manager.unified_update('ComfyUI-Bar', 'unknown')

    assert result.result is False
    assert result.msg == (f"Cannot update 'ComfyUI-Bar': this nodepack was not installed through ComfyUI-Manager, or its "
                          f"installation is damaged, so the Manager cannot update it. Reinstalling deletes its folder "
                          f"({folder}) and installs the nodepack again.")


@pytest.mark.parametrize('version', [None, 'unknown', 'nightly', '1.0.1'])
def test_updating_an_invalid_installation_fails_the_same_whatever_version_is_asked(core, custom_nodes, version):
    folder = pack(custom_nodes / 'ComfyUI-Foo-main', 'comfyui-foo')
    manager = reload(core)

    result = manager.unified_update('comfyui-foo', version)

    assert result.result is False
    assert result.msg.startswith("Cannot update 'comfyui-foo': this nodepack was not installed through ComfyUI-Manager")
    assert f'Reinstalling deletes its folder ({folder})' in result.msg


def test_a_registry_install_beside_a_stray_invalid_folder_is_updated_as_before(core, custom_nodes, monkeypatch):
    pack(custom_nodes / 'comfyui-foo', 'comfyui-foo', tracking=True)
    pack(custom_nodes / 'ComfyUI-Foo-main', 'comfyui-foo')
    registry_api(monkeypatch, core)
    manager = reload(core)

    result = manager.unified_update('comfyui-foo')

    assert (result.result, result.action, result.msg) == (True, 'skip', 'Up to date')


# --- uninstall

@pytest.mark.parametrize('node_id, is_unknown', [('comfyui-foo', False), ('ComfyUI-Bar', True)])
def test_uninstall_removes_the_invalid_folders_of_the_pack(core, custom_nodes, node_id, is_unknown):
    pack(custom_nodes / 'comfyui-foo', 'comfyui-foo')
    pack(custom_nodes / 'ComfyUI-Foo-main', 'comfyui-foo')
    pack(custom_nodes / 'ComfyUI-Bar')
    pack(custom_nodes / 'ComfyUI-Baz-valid', 'comfyui-baz', tracking=True)
    manager = reload(core)

    result = manager.unified_uninstall(node_id, is_unknown)

    assert result.result is True
    left = {'comfyui-foo': {'ComfyUI-Bar', 'ComfyUI-Baz-valid'},
            'ComfyUI-Bar': {'comfyui-foo', 'ComfyUI-Foo-main', 'ComfyUI-Baz-valid'}}[node_id]
    assert {path.name for path in custom_nodes.iterdir()} == left


def test_an_invalid_folder_that_cannot_be_deleted_is_deleted_at_the_next_start(core, custom_nodes, monkeypatch):
    folder = pack(custom_nodes / 'ComfyUI-Bar')
    manager = reload(core)
    monkeypatch.setattr(core.shutil, 'rmtree', Mock(side_effect=PermissionError('locked')))
    monkeypatch.setattr(core.os, 'rename', Mock(side_effect=PermissionError('locked')), raising=True)
    monkeypatch.setattr(core.time, 'sleep', lambda seconds: None)
    reserved = []
    monkeypatch.setattr(core, 'reserve_script', lambda title, cmd: reserved.append(cmd))

    result = manager.unified_uninstall('ComfyUI-Bar', True)

    assert result.result is True
    assert reserved == [['#LAZY-DELETE-NODEPACK', str(folder)]]


# --- reinstall

@pytest.mark.parametrize('folder', ['comfyui-foo', 'ComfyUI-Foo-main'])
def test_reinstall_makes_a_registry_install(core, custom_nodes, monkeypatch, folder):
    pack(custom_nodes / folder, 'comfyui-foo')
    registry_api(monkeypatch, core)
    manager = reload(core)

    result = asyncio.run(manager.reinstall_by_id('comfyui-foo', '1.0.1'))

    assert result.result is True, result.msg
    assert [path.name for path in custom_nodes.iterdir()] == ['comfyui-foo']
    assert (custom_nodes / 'comfyui-foo/.tracking').exists()
    reload(core)
    assert core.get_installed_node_packs() == {
        'comfyui-foo': {'ver': '1.0.1', 'cnr_id': 'comfyui-foo', 'aux_id': None, 'enabled': True}}
    assert flagged_rows(core) == {}


def test_reinstall_makes_a_git_install_of_a_non_registry_pack(core, custom_nodes, monkeypatch):
    pack(custom_nodes / 'ComfyUI-Bar')
    git_clone_stand_in(monkeypatch, core)
    manager = reload(core)

    result = asyncio.run(manager.reinstall_by_id('ComfyUI-Bar', 'unknown', core.DEFAULT_CHANNEL, 'cache'))

    assert result.result is True, result.msg
    assert (custom_nodes / 'ComfyUI-Bar/.git').is_dir()
    assert flagged_rows(core) == {}


def test_reinstall_refused_by_the_flagged_policy_keeps_the_folder(core, custom_nodes, monkeypatch):
    folder = pack(custom_nodes / 'ComfyUI-Foo-main', 'comfyui-foo')
    registry_api(monkeypatch, core, status='NodeVersionStatusFlagged')
    manager = reload(core)

    result = asyncio.run(manager.reinstall_by_id('comfyui-foo', '1.0.1'))

    assert result.result is False
    assert folder.exists()


# --- the queue of the v4 server

def data_models(monkeypatch):
    spec = importlib.util.spec_from_file_location('_generated_models', PACKAGE / 'data_models/generated_models.py')
    models = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, models)
    spec.loader.exec_module(models)
    return models


def run_glob_worker(monkeypatch, core, task, allowed_levels=('middle', 'middle+')):
    """Run the task worker of the v4 server on one task; return what it reported as done."""
    models = data_models(monkeypatch)
    done = []

    class Queue:
        tasks = [(task, 0)]
        running_tasks = {}

        def get(self, timeout):
            return self.tasks.pop() if self.tasks else None

        def total_count(self):
            return len(self.tasks)

        def done_count(self):
            return len(done)

        def finalize(self):
            pass

        async def task_done(self, item, index, msg, status):
            done.append((item.kind, msg, status.status_str))

    namespace = {name: getattr(models, name) for name in dir(models) if not name.startswith('_')}
    namespace.update(
        core=core, logging=logging, traceback=traceback_module(), os=os, task_queue=Queue(),
        manager_util=SimpleNamespace(clear_pip_cache=lambda: None),
        security_utils=SimpleNamespace(is_allowed_security_level=lambda level: level in allowed_levels),
        SECURITY_MESSAGE_MIDDLE='', SECURITY_MESSAGE_MIDDLE_P='',
        PromptServer=SimpleNamespace(instance=SimpleNamespace(send_sync=lambda *args: None)),
    )
    path = PACKAGE / 'glob/manager_server.py'
    nodes = [node for node in ast.parse(path.read_text(encoding='utf-8')).body
             if isinstance(node, ast.AsyncFunctionDef) and node.name == 'task_worker']
    exec(compile(ast.Module(nodes, []), str(path), 'exec'), namespace)
    asyncio.run(namespace['task_worker']())
    return done


def traceback_module():
    import traceback
    return traceback


def reinstall_task(monkeypatch):
    models = data_models(monkeypatch)
    return models.QueueTaskItem.model_validate({
        'ui_id': 'ui', 'client_id': 'client', 'kind': 'reinstall',
        'params': {'id': 'comfyui-foo', 'version': '1.0.1', 'selected_version': 'latest',
                   'mode': 'cache', 'channel': 'default'}})


@pytest.mark.parametrize('core', ['glob'], indirect=True)
def test_the_v4_queue_reinstalls(core, custom_nodes, monkeypatch):
    pack(custom_nodes / 'ComfyUI-Foo-main', 'comfyui-foo')
    registry_api(monkeypatch, core)

    done = run_glob_worker(monkeypatch, core, reinstall_task(monkeypatch))

    assert [(kind, msg) for kind, msg, status in done] == [('reinstall', 'success')]
    assert [path.name for path in custom_nodes.iterdir()] == ['comfyui-foo']
    assert (custom_nodes / 'comfyui-foo/.tracking').exists()


@pytest.mark.parametrize('core', ['glob'], indirect=True)
def test_the_v4_queue_checks_a_reinstall_as_an_install(core, custom_nodes, monkeypatch):
    folder = pack(custom_nodes / 'ComfyUI-Foo-main', 'comfyui-foo')
    registry_api(monkeypatch, core)

    done = run_glob_worker(monkeypatch, core, reinstall_task(monkeypatch), allowed_levels=())

    assert [(kind, msg) for kind, msg, status in done] == [('reinstall', 'failed')]
    assert folder.exists()


@pytest.mark.parametrize('core', ['glob'], indirect=True)
def test_the_v4_queue_says_why_an_update_of_an_invalid_installation_fails(core, custom_nodes, monkeypatch):
    # a client that does not know the flag takes the `ver: unknown` entry of a registry pack for a nightly
    folder = pack(custom_nodes / 'ComfyUI-Foo-main', 'comfyui-foo')
    models = data_models(monkeypatch)
    task = models.QueueTaskItem.model_validate({
        'ui_id': 'ui', 'client_id': 'client', 'kind': 'update',
        'params': {'node_name': 'comfyui-foo', 'node_ver': 'nightly'}})

    done = run_glob_worker(monkeypatch, core, task)

    assert len(done) == 1
    kind, msg, status = done[0]
    assert (kind, status) == ('update', 'error')
    assert "Cannot update 'comfyui-foo': this nodepack was not installed through ComfyUI-Manager" in msg
    assert f'Reinstalling deletes its folder ({folder})' in msg


@pytest.mark.parametrize('core', ['glob'], indirect=True)
def test_the_v4_installed_map_carries_the_flag(core, custom_nodes, monkeypatch):
    pack(custom_nodes / 'ComfyUI-Foo-main', 'comfyui-foo')
    pack(custom_nodes / 'comfyui-baz', 'comfyui-baz', version='2.0.0', tracking=True)
    reload(core)
    models = data_models(monkeypatch)

    state = models.TaskStateMessage(history={}, running_queue=[], pending_queue=[],
                                    installed_packs=core.get_installed_node_packs())

    packs = state.model_dump(mode='json')['installed_packs']
    assert packs['ComfyUI-Foo-main']['invalid_installation'] is True
    assert packs['ComfyUI-Foo-main']['cnr_id'] == 'comfyui-foo'
    assert not packs['comfyui-baz']['invalid_installation']


def test_the_console_line_of_a_failed_update_says_why(monkeypatch, caplog, core, custom_nodes):
    server = core.__name__.split('.')[-2]
    pack(custom_nodes / 'ComfyUI-Bar')
    reload(core)
    models = data_models(monkeypatch)
    namespace = {'core': core, 'logging': logging, 'os': os, 'traceback': traceback_module(),
                 'manager_util': SimpleNamespace(clear_pip_cache=lambda: None),
                 'OperationResult': models.OperationResult, 'UpdatePackParams': models.UpdatePackParams}
    path = PACKAGE / server / 'manager_server.py'
    nodes = [node for node in ast.walk(ast.parse(path.read_text(encoding='utf-8')))
             if isinstance(node, ast.AsyncFunctionDef) and node.name == 'do_update']
    exec(compile(ast.Module(nodes, []), str(path), 'exec'), namespace)
    item = (models.UpdatePackParams(node_name='ComfyUI-Bar', node_ver='unknown') if server == 'glob'
            else ('ui', 'ComfyUI-Bar', 'unknown'))

    asyncio.run(namespace['do_update'](item))

    errors = [record.getMessage() for record in caplog.records if record.levelno == logging.ERROR]
    assert len(errors) == 1
    assert "An error occurred while updating 'ComfyUI-Bar'" in errors[0]
    assert 'not installed through ComfyUI-Manager, or its installation is damaged' in errors[0]


# --- what a user reads

def test_what_a_user_reads_does_not_name_git_or_tracking(core, custom_nodes, capsys):
    pack(custom_nodes / 'ComfyUI-Bar')
    manager = reload(core)

    reason = manager.unified_update('ComfyUI-Bar', 'unknown').msg
    core.check_invalid_nodes()
    notice = capsys.readouterr().out

    for text in (reason, notice):
        assert 'not installed through ComfyUI-Manager' in text
        assert '.git' not in text and '.tracking' not in text
        assert not re.search(r'node\s+pack', text, re.IGNORECASE)


def legacy_page():
    """Render an invalid row of the legacy node list with the shipped JS, click its marker, ask to reinstall it."""
    js = JsSource(PACKAGE / 'js')
    manager = js.text('custom-nodes-manager.js')
    formatter = js.lift_formatter('custom-nodes-manager.js', "id: 'title',")
    action_buttons = slice_braced(manager, 'getActionButtons(action, rowItem, is_selected_button)')
    reinstall_confirmation = slice_braced(manager, 'if(mode === "reinstall")')
    script = '\n'.join([
        js.lift_declaration('common.js', 'export function sanitizeHTML('),
        line_containing(manager, 'const INVALID_INSTALLATION_GUIDANCE ='),
        'const messages = [], confirmations = [];',
        'const show_message = (msg) => messages.push(msg);',
        'const customConfirm = async (msg) => { confirmations.push(msg); return false; };',
        'const sanitizeUrl = (url) => url;',
        'const self = {};',
        'const document = {createElement: (tag) => ({tag, style: {}, children: [], '
        'appendChild(child) { this.children.push(child); }})};',
        f'const formatter = {formatter};',
        f'function {action_buttons}',
        f'async function confirm(mode, title, list) {{ {reinstall_confirmation} }}',
        "const row = {action: 'invalid-installation', title: 'ComfyUI-Foo', version: 'unknown', "
        "reference: 'https://github.com/example/ComfyUI-Foo', originalData: {id: 'comfyui-foo'}};",
        'const cell = formatter(row.title, row, {});',
        'const marker = cell.children[0];',
        'marker.onclick();',
        "await confirm('reinstall', row.title, ['hash']);",
        'console.log(JSON.stringify({cell: cell.children.map((child) => child.tag), '
        'marker: {label: marker.innerText, tooltip: marker.title}, dialog: messages, '
        'buttons: getActionButtons(row.action, row), confirmations}));',
    ])
    return run_node(script)


@pytest.mark.skipif(NODE is None, reason='node is required to execute the lifted production JS')
def test_the_legacy_page_explains_an_invalid_installation():
    page = legacy_page()
    buttons = parse(page['buttons'])

    # the marker sits on its own line above the title, like IMPORT FAILED
    assert page['cell'] == ['button', 'br', 'a']
    assert page['marker'] == {'label': 'INVALID INSTALLATION \u2197', 'tooltip': GUIDANCE}
    [dialog] = page['dialog']
    assert "The 'ComfyUI-Foo' nodepack is an invalid installation." in dialog
    assert GUIDANCE in dialog
    # the row offers Reinstall only, and the button says what it does
    assert buttons.tags == ['button']
    assert dict(buttons.attrs)['title'] == GUIDANCE
    assert page['confirmations'] == ['Reinstall ComfyUI-Foo? This deletes its folder and installs the nodepack again.']

    for text in (page['marker']['label'], page['marker']['tooltip'], dialog, page['buttons'], *page['confirmations']):
        assert '.git' not in text and '.tracking' not in text
        assert not re.search(r'node\s+pack', text, re.IGNORECASE)
        assert '(INVALID)' not in text
