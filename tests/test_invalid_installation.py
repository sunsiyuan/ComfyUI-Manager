"""A nodepack folder with neither .git nor .tracking is an invalid installation.

Such a folder comes from an install that broke midway, or from a download that somebody
unpacked into custom_nodes by hand: the Manager can neither update it nor tell its version.
The cases run in the isolated child of tests/test_install_flags_config.py, against the real
glob/manager_core scanning a temporary custom_nodes. The registry, the node list of the
channel, the archive download and the git clone are stand-ins.
"""
import re
from pathlib import Path

import pytest

from js_lift import NODE, JsSource, line_containing, parse, run_node, slice_braced
from test_install_flags_config import _run_child

PRELUDE = '''
import ast, asyncio, logging, pathlib, zipfile
from types import SimpleNamespace
from unittest.mock import Mock
import cnr_utils
core = manager_core
nodes = pathlib.Path(tmp) / 'custom_nodes'
nodes.mkdir()
sys.modules['folder_paths'].get_folder_paths = lambda name: [str(nodes)]
core.get_default_custom_nodes_path = lambda: str(nodes)
config = {'network_mode': 'public', 'channel_url': core.DEFAULT_CHANNEL, 'http_channel_enabled': False,
          'allow_flagged_nodepack_install': False}
core.get_config = lambda: config
core.valid_channels.add(core.DEFAULT_CHANNEL)
FOO_URL = 'https://github.com/example/ComfyUI-Foo'
BAR_URL = 'https://github.com/example/ComfyUI-Bar'
REGISTRY = [
    {'id': 'comfyui-foo', 'name': 'Foo', 'repository': FOO_URL, 'publisher': {'name': 'example'},
     'description': '', 'latest_version': {'version': '1.0.1'}},
    {'id': 'comfyui-baz', 'name': 'Baz', 'repository': 'https://github.com/example/comfyui-baz',
     'publisher': {'name': 'example'}, 'description': '', 'latest_version': {'version': '2.0.0'}},
]
NODE_LIST = [
    {'author': 'example', 'title': 'ComfyUI-Foo', 'reference': FOO_URL, 'files': [FOO_URL],
     'install_type': 'git-clone', 'description': ''},
    {'author': 'example', 'title': 'ComfyUI-Bar', 'reference': BAR_URL, 'files': [BAR_URL],
     'install_type': 'git-clone', 'description': ''},
]

async def get_cnr_data(*args, **kwargs):
    return REGISTRY

async def get_data_by_mode(mode, filename, channel_url=None):
    return {'custom_nodes': [dict(item) for item in NODE_LIST]}

cnr_utils.get_cnr_data = get_cnr_data
core.get_data_by_mode = get_data_by_mode
manager = core.UnifiedManager()
core.unified_manager = manager
manager.execute_install_script = Mock(return_value=True)

def pack(name, project_name=None, version='1.0.1', tracking=False, git_url=None):
    path = nodes / name
    path.mkdir(parents=True)
    (path / '__init__.py').write_text('NODE_CLASS_MAPPINGS = {}\\n')
    if project_name is not None:
        (path / 'pyproject.toml').write_text(f'[project]\\nname = "{project_name}"\\nversion = "{version}"\\n')
    if tracking:
        (path / '.tracking').write_text('__init__.py\\npyproject.toml\\n')
    if git_url is not None:
        (path / '.git').mkdir()
        (path / '.git/HEAD').write_text('0123456789abcdef0123456789abcdef01234567\\n')
        (path / '.git/config').write_text(f'[remote "origin"]\\n\\turl = {git_url}\\n')
    return path

def reload():
    asyncio.run(manager.reload('cache'))

def flagged_rows():
    # the rows of the node list that carry the flag, with their state
    rows = asyncio.run(core.get_unified_total_nodes(core.DEFAULT_CHANNEL, 'cache'))
    return {key: row['state'] for key, row in rows.items() if row.get('invalid-installation')}

def git_pack(name, origin_name):
    # a git clone of a local repository, one commit behind it
    import subprocess
    origin = pathlib.Path(tmp) / 'origins' / origin_name
    origin.mkdir(parents=True)

    def git(*args, cwd, date='2024-01-01T00:00:00'):
        subprocess.run(['git', '-c', 'user.name=test', '-c', 'user.email=test@example.invalid', *args], cwd=cwd,
                       env=dict(os.environ, GIT_AUTHOR_DATE=date, GIT_COMMITTER_DATE=date), check=True, capture_output=True)

    git('init', '-q', cwd=origin)
    for version in (1, 2):
        (origin / '__init__.py').write_text(f'VERSION = {version}\\n')
        git('add', '.', cwd=origin)
        # an update is a remote commit that is newer
        git('commit', '-q', '-m', f'version {version}', cwd=origin, date=f'2024-01-0{version}T00:00:00')
    git('clone', '-q', str(origin), str(nodes / name), cwd=origin.parent)
    git('reset', '-q', '--hard', 'HEAD~1', cwd=nodes / name)

def run_update_all():
    # Update All of the server; the packs it queued an update for
    queued = []

    async def save_snapshot_with_postfix(postfix):
        pass

    async def body():
        return {'mode': 'cache'}

    core.save_snapshot_with_postfix = save_snapshot_with_postfix
    update_all = server_function(
        'update_all', core=core, os=os, logging=logging, task_worker_thread=None,
        task_worker_lock=Mock(__enter__=Mock(), __exit__=Mock(return_value=False)),
        _reject_non_json_content_type=lambda request: None, is_allowed_security_level=lambda level: True,
        task_queue=SimpleNamespace(put=lambda item: queued.append(item[1][1])),
        web=SimpleNamespace(Response=lambda status: status))
    assert asyncio.run(update_all(SimpleNamespace(json=body))) == 200
    return sorted(queued)

def left():
    return sorted(path.name for path in nodes.iterdir())

def registry_api(status='NodeVersionStatusActive'):
    cnr_utils.requests.get = Mock(return_value=SimpleNamespace(status_code=200, json=lambda: {
        'id': 'version-id', 'node_id': 'comfyui-foo', 'version': '1.0.1', 'status': status,
        'downloadUrl': 'https://example.invalid/comfyui-foo.zip'}))
    core.manager_funcs.is_flagged_install_allowed = lambda *args: False

    def download(url, directory, name):
        with zipfile.ZipFile(pathlib.Path(directory) / name, 'w') as archive:
            archive.writestr('__init__.py', 'NODE_CLASS_MAPPINGS = {}  # 1.0.1\\n')
            archive.writestr('pyproject.toml', '[project]\\nname = "comfyui-foo"\\nversion = "1.0.1"\\n')

    core.manager_downloader.download_url = download

def server_function(name, **namespace):
    path = pathlib.Path(manager_core.__file__).parent / 'manager_server.py'
    found = [node for node in ast.walk(ast.parse(path.read_text(encoding='utf-8')))
             if isinstance(node, ast.AsyncFunctionDef) and node.name == name]
    for node in found:
        node.decorator_list = []
    namespace = dict(namespace)
    exec(compile(ast.Module(found, []), str(path), 'exec'), namespace)
    return namespace[name]

def emit(value):
    # on a line of its own: an update prints 'Updating: ...' without a line end
    print()
    print(json.dumps(value))
'''
INVALID = {'ver': 'unknown', 'aux_id': None, 'invalid_installation': True}
# what a user reads about an invalid installation, wherever it is shown
GUIDANCE = ('This nodepack was not installed through ComfyUI-Manager, or its installation is damaged, so the Manager '
            'cannot update it. Reinstalling deletes its folder and installs the nodepack again.')


def run(body):
    return _run_child(PRELUDE + body)


@pytest.mark.parametrize('folder, project_name, cnr_id, row', [
    ('comfyui-foo', 'comfyui-foo', 'comfyui-foo', 'comfyui-foo'),           # a registry install whose .tracking is gone
    ('ComfyUI-Foo-main', 'ComfyUI-Foo', 'comfyui-foo', 'comfyui-foo'),      # a GitHub zip unpacked by hand
    ('comfyui-baz', 'comfyui-foo', 'comfyui-foo', 'comfyui-foo'),           # rule 1 before rule 2
    ('comfyui-baz', 'not-in-the-registry', 'comfyui-baz', 'comfyui-baz'),   # rule 2
    ('ComfyUI-Baz', None, 'comfyui-baz', 'comfyui-baz'),                    # rule 2, ids compare case-insensitively
    ('ComfyUI-Bar', None, '', 'ComfyUI-Bar'),                               # rule 3, a non-registry row by its key
    ('Something-Else', None, '', None),                                     # rule 3, no row has that key
], ids=['no-tracking', 'github-zip', 'project-name-first', 'folder-name', 'folder-name-case', 'non-registry', 'no-row'])
def test_the_pack_of_an_invalid_installation(folder, project_name, cnr_id, row):
    data = run(f'''
pack({folder!r}, {project_name!r})
reload()
emit({{'installed': core.get_installed_node_packs(), 'rows': flagged_rows()}})
''')
    assert data['installed'] == {folder: {**INVALID, 'cnr_id': cnr_id, 'enabled': True}}
    # ComfyUI loads the folder: the row is installed, whether it is a registry row or not
    assert data['rows'] == ({row: 'enabled'} if row else {})


@pytest.mark.parametrize('folder, project_name, before, after', [
    ('comfyui-foo', 'comfyui-foo', 'comfyui-foo', 'comfyui-foo'),                     # a registry install whose .tracking is gone
    ('ComfyUI-Foo-main', 'ComfyUI-Foo', 'comfyui-foo', 'comfyui-foo'),                # a GitHub zip, the name as read_cnr_info has it
    ('comfyui-baz', 'not-in-the-registry', 'not-in-the-registry', 'comfyui-baz'),     # rule 2 needs the list
    ('Something-Else', 'something-else', 'something-else', 'something-else'),         # a name the registry does not have
    ('comfyui-baz', None, '', 'comfyui-baz'),                                         # no pyproject.toml: the folder name needs the list
    ('ComfyUI-Bar', None, '', ''),
], ids=['no-tracking', 'github-zip', 'folder-name', 'not-in-the-registry', 'no-pyproject', 'non-registry'])
def test_the_installed_map_tells_the_pack_before_the_registry_list_is_loaded(folder, project_name, before, after):
    """The page may ask for the installed map while the registry list is still loading. A pack that its own
    pyproject.toml names is told without the list, as the entry of a registry install is."""
    data = run(f'''
pack({folder!r}, {project_name!r})
assert not manager.cnr_map
before = core.get_installed_node_packs()
reload()
emit({{'before': before, 'after': core.get_installed_node_packs()}})
''')
    assert data['before'] == {folder: {**INVALID, 'cnr_id': before, 'enabled': True}}
    assert data['after'] == {folder: {**INVALID, 'cnr_id': after, 'enabled': True}}


@pytest.mark.parametrize('folder, project_name, cnr_id, row', [
    ('ComfyUI-Bar', None, '', 'ComfyUI-Bar'), ('ComfyUI-Foo-main', 'comfyui-foo', 'comfyui-foo', 'comfyui-foo'),
], ids=['non-registry', 'registry'])
def test_a_disabled_invalid_installation(folder, project_name, cnr_id, row):
    data = run(f'''
pack('.disabled/' + {folder!r}, {project_name!r})
reload()
emit({{'installed': core.get_installed_node_packs(), 'rows': flagged_rows()}})
''')
    assert data['installed'] == {folder: {**INVALID, 'cnr_id': cnr_id, 'enabled': False}}
    assert data['rows'] == {row: 'disabled'}


def test_what_is_never_an_invalid_installation():
    data = run('''
import contextlib, io
pack('comfyui-foo', 'comfyui-foo', tracking=True)
pack('ComfyUI-Bar', git_url=BAR_URL)
pack('not-in-the-registry', 'not-in-the-registry', version='3.0.0', tracking=True)
pack('tracking-without-pyproject', tracking=True)
pack('comfyui-manager', 'comfyui-manager')
(nodes / '__pycache__').mkdir()
(nodes / '.disabled').mkdir()
(nodes / 'single_file_node.py').write_text('')
reload()
out = io.StringIO()
with contextlib.redirect_stdout(out):
    core.check_invalid_nodes()
emit({'installed': core.get_installed_node_packs(), 'rows': flagged_rows(), 'notice': out.getvalue()})
''')
    assert data['installed'] == {
        # what these entries were before invalid installations were recognised
        'comfyui-foo': {'ver': '1.0.1', 'cnr_id': 'comfyui-foo', 'aux_id': None, 'enabled': True},
        'ComfyUI-Bar': {'ver': '0123456789abcdef0123456789abcdef01234567', 'cnr_id': '',
                        'aux_id': 'example/ComfyUI-Bar', 'enabled': True},
        'not-in-the-registry': {'ver': '3.0.0', 'cnr_id': 'not-in-the-registry', 'aux_id': None, 'enabled': True},
    }
    assert data['rows'] == {}
    assert data['notice'] == ''


def test_a_reload_forgets_a_folder_that_is_gone():
    data = run('''
folder = pack('comfyui-foo', 'comfyui-foo')
before = flagged_rows()
(folder / '.tracking').write_text('__init__.py\\n')
emit({'before': before, 'after': flagged_rows()})
''')
    assert data == {'before': {'comfyui-foo': 'enabled'}, 'after': {}}


def test_disable_and_enable_by_the_pack_id():
    data = run('''
pack('ComfyUI-Foo-main', 'comfyui-foo')
reload()
disabled = manager.unified_disable('comfyui-foo', True).result
after_disable = core.get_installed_node_packs()
enabled = manager.unified_enable('comfyui-foo', 'unknown').result
emit({'disabled': disabled, 'after_disable': after_disable, 'enabled': enabled,
                  'after_enable': core.get_installed_node_packs()})
''')
    assert data['disabled'] is True and data['enabled'] is True
    assert data['after_disable'] == {'comfyui-foo': {**INVALID, 'cnr_id': 'comfyui-foo', 'enabled': False}}
    assert data['after_enable'] == {'comfyui-foo': {**INVALID, 'cnr_id': 'comfyui-foo', 'enabled': True}}


def test_the_notice_lists_the_invalid_folders_that_a_reload_found():
    data = run('''
import contextlib, io
folder = pack('ComfyUI-Bar')
reload()
out = io.StringIO()
with contextlib.redirect_stdout(out):
    core.check_invalid_nodes()
emit({'folder': str(folder), 'out': out.getvalue()})
''')
    assert 'Nodes requiring reinstallation have been detected' in data['out']
    assert 'not installed through ComfyUI-Manager, or their installation is damaged' in data['out']
    assert data['folder'] in data['out']


def test_fetching_updates_leaves_an_invalid_installation_alone():
    data = run('''
pack('ComfyUI-Bar')
reload()
emit(manager.fetch_or_pull_git_repo(is_pull=True))
''')
    assert data == {'updated': [], 'failed': []}


def test_fetching_updates_reaches_a_git_pack_whose_id_a_stray_invalid_folder_shares():
    data = run('''
git_pack('bar-clone', 'ComfyUI-Bar')
pack('.disabled/ComfyUI-Bar')
reload()
emit(manager.fetch_or_pull_git_repo(is_pull=False))
''')
    assert data == {'updated': ['ComfyUI-Bar@unknown'], 'failed': []}


def test_update_all_queues_no_update_for_an_invalid_installation():
    data = run('''
pack('ComfyUI-Bar')
pack('ComfyUI-Foo-main', 'comfyui-foo')
pack('comfyui-baz', 'comfyui-baz', version='2.0.0', tracking=True)
pack('ComfyUI-Qux', git_url='https://github.com/example/ComfyUI-Qux')
emit(run_update_all())
''')
    assert data == ['ComfyUI-Qux', 'comfyui-baz']


def test_update_all_updates_a_git_pack_whose_id_a_stray_invalid_folder_shares():
    data = run('''
pack('bar-clone', git_url=BAR_URL)
pack('.disabled/ComfyUI-Bar')
emit(run_update_all())
''')
    assert data == ['ComfyUI-Bar']


def test_updating_an_invalid_installation_says_why_it_fails():
    data = run('''
folder = pack('ComfyUI-Bar')
reload()
result = manager.unified_update('ComfyUI-Bar', 'unknown')
emit({'ok': result.result, 'msg': result.msg, 'folder': str(folder)})
''')
    assert data['ok'] is False
    assert data['msg'] == (f"Cannot update 'ComfyUI-Bar': this nodepack was not installed through ComfyUI-Manager, or "
                           f"its installation is damaged, so the Manager cannot update it. Reinstalling deletes its "
                           f"folder ({data['folder']}) and installs the nodepack again.")


@pytest.mark.parametrize('version', [None, 'unknown', 'nightly', '1.0.1'])
def test_updating_an_invalid_installation_fails_the_same_whatever_version_is_asked(version):
    data = run(f'''
folder = pack('ComfyUI-Foo-main', 'comfyui-foo')
reload()
result = manager.unified_update('comfyui-foo', {version!r})
emit({{'ok': result.result, 'msg': result.msg, 'folder': str(folder)}})
''')
    assert data['ok'] is False
    assert data['msg'].startswith("Cannot update 'comfyui-foo': this nodepack was not installed through ComfyUI-Manager")
    assert f"Reinstalling deletes its folder ({data['folder']})" in data['msg']


def test_a_registry_install_beside_a_stray_invalid_folder_is_updated_as_before():
    data = run('''
pack('comfyui-foo', 'comfyui-foo', tracking=True)
pack('ComfyUI-Foo-main', 'comfyui-foo')
registry_api()
reload()
result = manager.unified_update('comfyui-foo')
emit([result.result, result.action, result.msg])
''')
    assert data == [True, 'skip', 'Up to date']


def test_the_console_line_of_a_failed_update_says_why():
    data = run('''
pack('ComfyUI-Bar')
reload()
errors = []
handler = logging.Handler()
handler.emit = lambda record: errors.append(record.getMessage()) if record.levelno == logging.ERROR else None
logging.getLogger().addHandler(handler)
import traceback
do_update = server_function('do_update', core=core, os=os, logging=logging, traceback=traceback,
                            manager_util=SimpleNamespace(clear_pip_cache=lambda: None))
asyncio.run(do_update(('ui', 'ComfyUI-Bar', 'unknown')))
emit(errors)
''')
    assert len(data) == 1
    assert "An error occurred while updating 'ComfyUI-Bar'" in data[0]
    assert 'not installed through ComfyUI-Manager, or its installation is damaged' in data[0]


@pytest.mark.parametrize('node_id, is_unknown, left', [
    ('comfyui-foo', False, ['ComfyUI-Bar', 'ComfyUI-Baz-valid']),
    ('ComfyUI-Bar', True, ['ComfyUI-Baz-valid', 'ComfyUI-Foo-main', 'comfyui-foo']),
])
def test_uninstall_removes_the_invalid_folders_of_the_pack(node_id, is_unknown, left):
    data = run(f'''
pack('comfyui-foo', 'comfyui-foo')
pack('ComfyUI-Foo-main', 'comfyui-foo')
pack('ComfyUI-Bar')
pack('ComfyUI-Baz-valid', 'comfyui-baz', tracking=True)
reload()
result = manager.unified_uninstall({node_id!r}, {is_unknown!r})
emit({{'ok': result.result, 'left': left()}})
''')
    assert data == {'ok': True, 'left': left}


def test_an_invalid_folder_that_cannot_be_deleted_is_deleted_at_the_next_start():
    data = run('''
folder = pack('ComfyUI-Bar')
reload()
reserved = []
core.shutil.rmtree = Mock(side_effect=PermissionError('locked'))
core.reserve_script = lambda title, cmd: reserved.append(cmd)
core.time.sleep = lambda seconds: None
result = manager.unified_uninstall('ComfyUI-Bar', True)
emit({'ok': result.result, 'reserved': reserved, 'folder': str(folder)})
''')
    assert data['ok'] is True
    assert data['reserved'] == [['#LAZY-DELETE-NODEPACK', data['folder']]]


@pytest.mark.parametrize('folder', ['comfyui-foo', 'ComfyUI-Foo-main'])
def test_reinstall_makes_a_registry_install(folder):
    data = run(f'''
pack({folder!r}, 'comfyui-foo')
registry_api()
reload()
result = asyncio.run(manager.reinstall_by_id('comfyui-foo', '1.0.1'))
after = left()
tracking = (nodes / 'comfyui-foo/.tracking').exists()
reload()
emit({{'ok': result.result, 'msg': result.msg, 'left': after, 'tracking': tracking,
                   'installed': core.get_installed_node_packs(), 'rows': flagged_rows()}})
''')
    assert data['ok'] is True, data['msg']
    assert data['left'] == ['comfyui-foo']
    assert data['tracking'] is True
    assert data['installed'] == {'comfyui-foo': {'ver': '1.0.1', 'cnr_id': 'comfyui-foo', 'aux_id': None, 'enabled': True}}
    assert data['rows'] == {}


def test_reinstall_makes_a_git_install_of_a_non_registry_pack():
    data = run('''
pack('ComfyUI-Bar')
reload()

def clone(url, path, **kwargs):
    pathlib.Path(path).mkdir()
    (pathlib.Path(path) / '.git').mkdir()
    (pathlib.Path(path) / '.git/config').write_text(f'[remote "origin"]\\n\\turl = {url}\\n')
    return SimpleNamespace(git=SimpleNamespace(clear_cache=lambda: None), close=lambda: None)

core.git.Repo.clone_from = staticmethod(clone)
result = asyncio.run(manager.reinstall_by_id('ComfyUI-Bar', 'unknown', core.DEFAULT_CHANNEL, 'cache'))
emit({'ok': result.result, 'msg': result.msg, 'git': (nodes / 'ComfyUI-Bar/.git').is_dir(),
                  'rows': flagged_rows()})
''')
    assert data['ok'] is True, data['msg']
    assert data['git'] is True
    assert data['rows'] == {}


def test_reinstall_refused_by_the_flagged_policy_keeps_the_folder():
    data = run('''
folder = pack('ComfyUI-Foo-main', 'comfyui-foo')
registry_api(status='NodeVersionStatusFlagged')
reload()
result = asyncio.run(manager.reinstall_by_id('comfyui-foo', '1.0.1'))
emit({'ok': result.result, 'left': left()})
''')
    assert data == {'ok': False, 'left': ['ComfyUI-Foo-main']}


def test_the_installed_route_lists_an_invalid_installation():
    data = run('''
pack('ComfyUI-Foo-main', 'comfyui-foo')
reload()
installed_list = server_function('installed_list', core=core, startup_time_installed_node_packs={},
                                 web=SimpleNamespace(json_response=lambda body, content_type: body))
emit(asyncio.run(installed_list(SimpleNamespace(query={}))))
''')
    assert data == {'ComfyUI-Foo-main': {**INVALID, 'cnr_id': 'comfyui-foo', 'enabled': True}}


# --- what a user reads

def test_what_a_user_reads_does_not_name_git_or_tracking():
    data = run('''
import contextlib, io
pack('ComfyUI-Bar')
reload()
reason = manager.unified_update('ComfyUI-Bar', 'unknown').msg
out = io.StringIO()
with contextlib.redirect_stdout(out):
    core.check_invalid_nodes()
emit({'reason': reason, 'notice': out.getvalue()})
''')
    for text in (data['reason'], data['notice']):
        assert 'not installed through ComfyUI-Manager' in text
        assert '.git' not in text and '.tracking' not in text
        assert not re.search(r'node\s+pack', text, re.IGNORECASE)


def legacy_page():
    """Render an invalid row of the legacy node list with the shipped JS, click its marker, ask to reinstall it."""
    js = JsSource(Path(__file__).resolve().parent.parent / 'js')
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
