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
        pin = yaml.safe_load((folder / 'release.yaml').read_text())['clearsignage_revision']
        assert pin == '' or (len(pin) == 40 and all(c in '0123456789abcdef' for c in pin))
        for name in ('run', 'finish'):
            assert os.access(folder / 'rootfs/etc/services.d/clearvenue' / name, os.X_OK)


@pytest.mark.parametrize('channel', CHANNELS)
def test_gate_allows_mapped_branch_and_requires_exact_override_pin(channel):
    gate = load('check-publish-allowed')
    settings = channel_settings(channel)
    args = dict(push=True, channel=channel, branch=settings['branch'], override='',
                built_revision='a' * 40, pinned_revision='')
    assert gate.publish_refusal(**args) is None
    assert gate.publish_refusal(**dict(args, branch='wrong'))
    assert gate.publish_refusal(**dict(args, built_revision=''))
    assert gate.publish_refusal(**dict(args, override='a' * 40))
    assert gate.publish_refusal(**dict(args, override='a' * 40, pinned_revision='a' * 40)) is None
    assert gate.publish_refusal(**dict(args, override='b' * 40, pinned_revision='a' * 40))
    assert gate.publish_refusal(**dict(args, push=False, override='a' * 40)) is None


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


def test_new_package_bootstrap_requires_successful_owner_listing(monkeypatch):
    def missing(url, token, method='GET'):
        if '/versions' in url:
            raise urllib.error.HTTPError(url, 404, 'missing', {}, None)
        return APIResponse([{'name': 'clearvenue'}])
    monkeypatch.setattr(ghcr_api, 'request', missing)
    assert ghcr_api.all_versions('owner', 'clearvenue-dev', 'test', owner_kind='organization', allow_missing=True) == []
    with pytest.raises(urllib.error.HTTPError):
        ghcr_api.all_versions('owner', 'clearvenue', 'test', owner_kind='organization', allow_missing=True)
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
    calls = []
    monkeypatch.setattr(ghcr_api, 'request',
                        lambda url, token, method='GET': calls.append(url) or APIResponse([]))
    assert ghcr_api.versions_url('an owner', 'an/app', kind) == (
        f'https://api.github.com/{route}/an%20owner/packages/container/an%2Fapp/versions')
    assert ghcr_api.package_names('an owner', 'token', owner_kind=kind) == set()
    assert calls == [f'https://api.github.com/{route}/an%20owner/packages?package_type=container&per_page=100']


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


def test_ci_shell_fragments_parse_and_workflow_uses_channels():
    import re
    pipeline = (ROOT / 'jenkinsfile-ha').read_text()
    assert "name: 'CHANNEL'" in pipeline
    assert "choices: ['stable', 'beta', 'dev']" in pipeline
    assert "name: 'CLEARSIGNAGE_REF'" not in pipeline
    assert 'RECORD_BRANCH=main' in pipeline
    assert '--channel "${CHANNEL}"' in pipeline
    fragments = re.findall("'''(.*?)'''", pipeline, re.S)
    workflow = yaml.safe_load((ROOT / '.github/workflows/homeassistant.yml').read_text())
    inputs = workflow[True]['workflow_dispatch']['inputs']  # PyYAML's YAML 1.1 boolean key
    assert inputs['channel']['options'] == ['stable', 'beta', 'dev']
    assert 'clearsignage_ref' not in inputs
    steps = workflow['jobs']['build']['steps']
    fragments += [step['run'] for step in steps if 'run' in step]
    for fragment in fragments:
        parsed = subprocess.run(['bash', '-n'], input=fragment, text=True, capture_output=True)
        assert parsed.returncode == 0, parsed.stderr
    assert next(step for step in steps if step['name'] == 'Require packaging main for publishing')['if'] == 'inputs.push'


@pytest.mark.parametrize('channel', CHANNELS)
@pytest.mark.parametrize('push', ['true', 'false'])
def test_jenkins_build_commands_publish_only_the_selected_image(tmp_path, channel, push):
    import re
    settings = channel_settings(channel)
    pipeline = (ROOT / 'jenkinsfile-ha').read_text()
    build_stage = pipeline.split("stage('Build and publish')", 1)[1].split("stage('Record the published version')", 1)[0]
    fragment = re.search("'''(.*?)'''", build_stage, re.S).group(1)
    # Execute the actual shell step with a fake Docker executable, never a daemon.
    binary_dir = tmp_path / 'bin'
    binary_dir.mkdir()
    docker = binary_dir / 'docker'
    docker.write_text('#!/bin/bash\nprintf "%s\\n" "$*" >> "$DOCKER_CALLS"\n')
    docker.chmod(0o755)
    venv = tmp_path / '.venv/bin'
    venv.mkdir(parents=True)
    (venv / 'python').symlink_to(sys.executable)
    app = tmp_path / settings['addon_dir']
    app.mkdir()
    (app / 'build.yaml').write_bytes((ROOT / settings['addon_dir'] / 'build.yaml').read_bytes())
    log = tmp_path / 'calls'
    env = {**os.environ, 'PATH': str(binary_dir) + os.pathsep + os.environ['PATH'],
           'ADDON_DIR': settings['addon_dir'], 'IMAGE': settings['image'], 'PUSH': push,
           'APP_VERSION': '20260929.01', 'RESOLVED_REF': 'a' * 40, 'REGISTRY': 'ghcr.io',
           'GHCR_USR': 'fixture', 'GHCR_PSW': 'fixture', 'DOCKER_CALLS': str(log)}
    result = subprocess.run(['bash'], input=fragment, cwd=tmp_path, env=env, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    calls = log.read_text().splitlines()
    builds = [line for line in calls if line.startswith('buildx build ')]
    assert len(builds) == 2
    for line, architecture in zip(builds, ['aarch64', 'amd64']):
        assert f"--tag {settings['image']}:20260929.01-{architecture}" in line
        assert line.endswith(' ' + settings['addon_dir'])
        assert ('--push' in line) == (push == 'true')
    manifests = [line for line in calls if 'imagetools create' in line]
    assert len(manifests) == (1 if push == 'true' else 0)
    if manifests:
        assert f"--tag {settings['image']}:latest" in manifests[0]


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
    monkeypatch.setattr(ghcr_api, 'package_names', lambda *args, **kwargs: {'clearsignage-ha', 'clearvenue', 'clearvenue-beta', 'clearvenue-dev'})
    monkeypatch.setattr(ghcr_api, 'request', lambda url, token, method: deleted.append((url, method)) or io.StringIO(''))
    monkeypatch.setenv('GHCR_TOKEN', 'fixture')
    monkeypatch.setattr(sys, 'argv', ['cleanup'] + (['--apply'] if apply else []))
    cleanup.main()
    assert deleted == ([
        ('https://api.github.com/repos/madeByJansen/ClearVenue-HA/releases/5', 'DELETE'),
        ('https://api.github.com/repos/madeByJansen/ClearVenue-HA/git/refs/tags/v20260928.01', 'DELETE'),
        ('https://api.github.com/orgs/madeByJansen/packages/container/clearsignage-ha', 'DELETE'),
    ] if apply else [])
