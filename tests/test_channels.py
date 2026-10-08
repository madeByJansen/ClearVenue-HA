"""Channel isolation, source resolution and safe legacy retirement."""
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import urllib.error
import urllib.request

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from channels import CHANNELS, channel_settings
import ghcr_api


class APIResponse(io.StringIO):
    def __init__(self, value, headers=None):
        super().__init__(json.dumps(value))
        self.headers = headers or {}


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def git(path, *args):
    return subprocess.check_output(['git', '-C', str(path), *args], text=True).strip()


@pytest.mark.parametrize('channel,branch,slug,package', [
    ('stable', 'prod', 'clearvenue', 'clearvenue'),
    ('beta', 'beta', 'clearvenue_beta', 'clearvenue-beta'),
    ('dev', 'main', 'clearvenue_dev', 'clearvenue-dev'),
])
def test_channel_identity_and_manifest(channel, branch, slug, package):
    settings = channel_settings(channel)
    assert settings['branch'] == branch
    assert settings['slug'] == settings['addon_dir'] == slug
    assert settings['package'] == package
    config = yaml.safe_load((ROOT / slug / 'config.yaml').read_text())
    assert config['slug'] == slug
    assert config['name'] == config['panel_title'] == settings['name']
    assert config['image'] == 'ghcr.io/madebyjansen/' + package
    assert config['url'] == 'https://github.com/madeByJansen/ClearVenue-HA'
    stable = yaml.safe_load((ROOT / 'clearvenue/config.yaml').read_text())
    for key in set(config) - {'name', 'slug', 'image', 'panel_title', 'version'}:
        assert config[key] == stable[key]


def test_only_three_apps_and_shared_packaging_is_in_sync():
    assert {p.parent.name for p in ROOT.glob('*/config.yaml')} == {'clearvenue', 'clearvenue_beta', 'clearvenue_dev'}
    subprocess.run([sys.executable, str(ROOT / 'scripts/sync-channel-packaging.py'), '--check'], check=True)
    for channel in CHANNELS:
        folder = ROOT / channel_settings(channel)['addon_dir']
        for name in ('run', 'finish'):
            assert os.access(folder / 'rootfs/etc/services.d/clearvenue' / name, os.X_OK)


def test_unknown_channel_fails_before_source_or_registry_access():
    with pytest.raises(ValueError):
        channel_settings('prod')
    result = subprocess.run(['bash', str(ROOT / 'scripts/fetch-source.sh')],
                            env={**os.environ, 'CHANNEL': '../../wrong'}, capture_output=True, text=True)
    assert result.returncode != 0
    assert 'Fetching' not in result.stdout


@pytest.fixture
def local_source(tmp_path):
    upstream = tmp_path / 'upstream'
    upstream.mkdir()
    git(upstream, 'init', '-q', '-b', 'main')
    required = ['device/requirements.txt', 'device/constraints.txt', 'device/app/main.py',
                'shared/pyproject.toml', 'hosted/__main__.py', 'clearvenue/__main__.py',
                'clearvenue/hosts/__init__.py', 'event_share/app/VERSION', 'event_share/install/install.php',
                'device/tests/test_private.py']
    for name in required:
        p = upstream / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text('fixture\n')
    shas = {}
    for branch in ('main', 'beta', 'prod'):
        if branch != 'main':
            git(upstream, 'checkout', '-qb', branch)
        (upstream / 'clearvenue/source_branch').write_text(branch)
        git(upstream, 'add', '.')
        git(upstream, '-c', 'user.name=Test', '-c', 'user.email=test@example.com', 'commit', '-qm', branch)
        shas[branch] = git(upstream, 'rev-parse', 'HEAD')
    packaging = tmp_path / 'packaging'
    import shutil
    shutil.copytree(ROOT / 'scripts', packaging / 'scripts')
    return upstream, packaging, shas


@pytest.mark.parametrize('channel', CHANNELS)
def test_fetch_maps_channel_and_exact_commit_without_touching_other_apps(local_source, channel):
    upstream, packaging, shas = local_source
    settings = channel_settings(channel)
    env = {**os.environ, 'CHANNEL': channel, 'CLEARSIGNAGE_REPO': str(upstream), 'CLEARSIGNAGE_REF_OVERRIDE': ''}
    # An obsolete Jenkins parameter must not silently redirect the channel.
    env['CLEARSIGNAGE_REF'] = 'wrong'
    command = ['bash', str(packaging / 'scripts/fetch-source.sh')]
    result = subprocess.run(command, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    source = packaging / settings['addon_dir'] / 'src'
    assert (source / 'CLEARSIGNAGE_REF').read_text().strip() == shas[settings['branch']]
    assert (source / 'clearvenue/source_branch').read_text() == settings['branch']
    assert not (source / 'device/tests').exists()
    # Kept, so the venue bundle is built from the same commit without fetching it again.
    assert git(packaging / '.upstream/clearsignage', 'rev-parse', 'HEAD') == shas[settings['branch']]
    assert len(list(packaging.glob('*/src'))) == 1
    env['CLEARSIGNAGE_REF_OVERRIDE'] = shas['main']
    result = subprocess.run(command, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert (source / 'CLEARSIGNAGE_REF').read_text().strip() == shas['main']
    env['CLEARSIGNAGE_REF_OVERRIDE'] = 'main'
    result = subprocess.run(command, env=env, capture_output=True, text=True)
    assert result.returncode != 0
    assert 'Fetching' not in result.stdout


def test_new_package_bootstrap_treats_missing_package_as_no_releases(monkeypatch):
    def missing(url, token, method='GET'):
        raise urllib.error.HTTPError(url, 404, 'missing', {}, None)
    monkeypatch.setattr(ghcr_api, 'request', missing)
    assert ghcr_api.all_versions('owner', 'clearvenue-dev', 'test', owner_kind='organization', allow_missing=True) == []
    with pytest.raises(urllib.error.HTTPError):
        ghcr_api.all_versions('owner', 'clearvenue-dev', 'test', owner_kind='organization')
    def denied(url, token, method='GET'):
        raise urllib.error.HTTPError(url, 403, 'forbidden', {}, None)
    monkeypatch.setattr(ghcr_api, 'request', denied)
    with pytest.raises(urllib.error.HTTPError):
        ghcr_api.all_versions('owner', 'clearvenue-dev', 'test', owner_kind='organization', allow_missing=True)


def test_ghcr_pagination_follows_link_and_stops_without_one(monkeypatch):
    next_url = 'https://api.github.com/orgs/owner/packages/container/app/versions?per_page=100&page=2'
    responses = [
        APIResponse([{'id': 1}], {'Link': f'<{next_url}>; rel="next", <ignored>; rel="last"'}),
        APIResponse([{'id': number} for number in range(2, 102)]),
    ]
    calls = []

    def request(url, token, method='GET'):
        calls.append(url)
        return responses.pop(0)

    monkeypatch.setattr(ghcr_api, 'request', request)
    versions = ghcr_api.all_versions('owner', 'app', 'token', owner_kind='organization')
    assert len(versions) == 101
    assert calls == [
        'https://api.github.com/orgs/owner/packages/container/app/versions?per_page=100',
        next_url,
    ]
    assert not responses


@pytest.mark.parametrize('kind,route', [('organization', 'orgs'), ('user', 'users')])
def test_package_urls_make_owner_kind_explicit(monkeypatch, kind, route):
    assert ghcr_api.versions_url('an owner', 'an/app', kind) == (
        f'https://api.github.com/{route}/an%20owner/packages/container/an%2Fapp/versions')


def test_http_error_reports_github_message_without_token(monkeypatch):
    token = 'super-secret-token'
    url = f'https://api.github.com/example?access_token={token}&page=1'
    body = io.BytesIO(json.dumps({'message': 'Bad owner route', 'documentation_url': 'https://docs.github.com'}).encode())

    def fail(request):
        assert request.headers['Authorization'] == f'Bearer {token}'
        raise urllib.error.HTTPError(request.full_url, 400, 'Bad Request', {}, body)

    monkeypatch.setattr(urllib.request, 'urlopen', fail)
    with pytest.raises(urllib.error.HTTPError) as caught:
        ghcr_api.request(url, token, method='DELETE')
    diagnostic = str(caught.value)
    assert 'DELETE https://api.github.com/example?access_token=REDACTED&page=1' in diagnostic
    assert 'status 400' in diagnostic
    assert 'Bad owner route' in diagnostic
    assert token not in diagnostic


def test_channel_chooser_reads_only_selected_package(monkeypatch, tmp_path):
    chooser = load('next-image-version')
    calls = []
    def versions(owner, package, token, **kwargs):
        calls.append(package)
        return [{'metadata': {'container': {'tags': ['20260929.03']}}}]
    monkeypatch.setattr(ghcr_api, 'all_versions', versions)
    for channel in CHANNELS:
        settings = channel_settings(channel)
        tags = chooser.published_tags('owner', settings['package'], 'test')
        assert chooser.next_version(tags, __import__('datetime').date(2026, 9, 29)) == '20260929.04'
    assert calls == ['clearvenue', 'clearvenue-beta', 'clearvenue-dev']


@pytest.mark.parametrize('apply', [False, True])
def test_pruning_targets_only_selected_channel(monkeypatch, apply):
    pruner = load('prune-ghcr-releases')
    calls = []
    versions = [{'id': i, 'metadata': {'container': {'tags': [tag]}}}
                for i, tag in enumerate(['20260927.01', '20260928.01', '20260929.01', '20260930.01'])]
    def listing(owner, package, token, *, owner_kind):
        assert owner_kind == 'organization'
        assert package == 'clearvenue-beta'
        return versions
    monkeypatch.setattr(ghcr_api, 'all_versions', listing)
    monkeypatch.setattr(ghcr_api, 'request', lambda url, token, method: calls.append((url, method)) or io.StringIO(''))
    monkeypatch.setenv('GHCR_TOKEN', 'test')
    monkeypatch.setattr(sys, 'argv', ['prune', '--channel', 'beta', '--current', '20260929.01'] + (['--apply'] if apply else []))
    assert pruner.main() == 0
    assert calls == ([('https://api.github.com/orgs/madeByJansen/packages/container/clearvenue-beta/versions/0', 'DELETE')] if apply else [])


def test_cleanup_waits_for_replacement_rollout():
    cleanup = load('cleanup-legacy-releases')
    manifests, tags = {}, {}
    for channel in CHANNELS:
        settings = channel_settings(channel)
        manifests[channel] = dict(slug=settings['slug'], image=settings['image'], version='20260929.01')
        tags[settings['package']] = {'20260929.01', '20260929.01-amd64', '20260929.01-aarch64'}
    cleanup.replacement_ready(set(), manifests, tags)
    with pytest.raises(ValueError, match='old slug'):
        cleanup.replacement_ready({'clearsignage/config.yaml'}, manifests, tags)
    tags['clearvenue-dev'].remove('20260929.01-amd64')
    with pytest.raises(ValueError, match='both architectures'):
        cleanup.replacement_ready(set(), manifests, tags)
    assert cleanup.LEGACY_TAG.fullmatch('v20260928.01')
    for tag in ('dev/v20260928.01', 'beta/v20260928.01', 'stable/v20260928.01', 'important'):
        assert not cleanup.LEGACY_TAG.fullmatch(tag)


def test_the_add_on_is_built_only_by_the_release_workflow_s_entry():
    """ClearSignage's one release workflow builds the add-on; nothing here does too."""
    assert not (ROOT / 'jenkinsfile-ha').exists()
    assert sorted(path.name for path in (ROOT / '.github/workflows').iterdir()) == ['tests.yml']
    for script in ('release-addon.sh', 'validate-packaging.sh'):
        parsed = subprocess.run(['bash', '-n', str(ROOT / 'scripts' / script)], capture_output=True, text=True)
        assert parsed.returncode == 0, parsed.stderr
        assert os.access(ROOT / 'scripts' / script, os.X_OK), f'{script} is not executable'


def test_every_change_here_runs_the_checks_a_release_runs_first():
    workflow = yaml.safe_load((ROOT / '.github/workflows/tests.yml').read_text())
    triggers = workflow[True]  # PyYAML's YAML 1.1 boolean key
    assert 'pull_request' in triggers and triggers['push']['branches'] == ['main']
    steps = workflow['jobs']['tests']['steps']
    assert any(step.get('run') == 'scripts/validate-packaging.sh' for step in steps)
    entry = (ROOT / 'scripts/release-addon.sh').read_text()
    assert entry.index('scripts/validate-packaging.sh"') < entry.index('scripts/fetch-source.sh"'), 'validated first'


@pytest.mark.parametrize('apply', [False, True])
def test_legacy_cleanup_deletes_only_old_artifacts(monkeypatch, apply):
    import base64
    cleanup = load('cleanup-legacy-releases')
    deleted = []
    def read(url, token):
        if url.endswith('/git/ref/heads/main'):
            return {'object': {'sha': 'a' * 40}}
        if '/git/trees/' in url:
            return {'tree': [], 'truncated': False}
        if '/contents/' in url:
            folder = url.split('/contents/')[1].split('/')[0]
            settings = next(channel_settings(c) for c in CHANNELS if channel_settings(c)['addon_dir'] == folder)
            content = yaml.safe_dump({'slug': settings['slug'], 'image': settings['image'], 'version': '20260929.01'})
            return {'content': base64.b64encode(content.encode()).decode()}
        raise AssertionError(url)
    monkeypatch.setattr(cleanup, 'read_json', read)
    monkeypatch.setattr(cleanup, 'collection', lambda url, token: (
        [{'id': 5, 'tag_name': 'v20260928.01'}, {'id': 6, 'tag_name': 'stable/v20260929.01'}]
        if url.endswith('/releases') else [{'name': 'v20260928.01'}, {'name': 'dev/v20260929.01'}]))
    monkeypatch.setattr(ghcr_api, 'all_versions', lambda *args, **kwargs: [
        {'metadata': {'container': {'tags': ['20260929.01', '20260929.01-amd64', '20260929.01-aarch64']}}}])
    monkeypatch.setattr(cleanup, 'package_exists', lambda package, token: package == 'clearsignage-ha')
    monkeypatch.setattr(ghcr_api, 'request', lambda url, token, method: deleted.append((url, method)) or io.StringIO(''))
    monkeypatch.setenv('GHCR_TOKEN', 'fixture')
    monkeypatch.setattr(sys, 'argv', ['cleanup'] + (['--apply'] if apply else []))
    cleanup.main()
    assert deleted == ([
        ('https://api.github.com/repos/madeByJansen/ClearVenue-HA/releases/5', 'DELETE'),
        ('https://api.github.com/repos/madeByJansen/ClearVenue-HA/git/refs/tags/v20260928.01', 'DELETE'),
        ('https://api.github.com/orgs/madeByJansen/packages/container/clearsignage-ha', 'DELETE'),
    ] if apply else [])
