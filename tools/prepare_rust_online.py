"""Prepare loopback HTTP acceptance origins; never use these test keys in production."""
import argparse
import json
from pathlib import Path
import shutil
from installer_prototype.common import atomic_json, keygen
from installer_prototype.registry_updates import publish_registry
from installer_prototype.repository import publish
from installer_prototype.tuf_repository import demo_trust
from prepare_rust_fixture import prepare
from check_native_tuf import generate


def online(work, ifw, real=False, port=18765, roots=1030):
    work = Path(work).resolve()
    if not real:
        prepare(ifw, work)
    home = work if real else work / 'demo'
    source = json.loads((home / 'registry-source.json').read_text())
    target = source['target']
    origin = home / 'store' / target
    registry = json.loads((origin / 'registry/registry.json').read_text())['signed']['registry']
    base = f'http://127.0.0.1:{port}/{target}'
    for name, repo in registry['repositories'].items():
        repo['url'] = base + '/' + name
        repo['tuf']['url'] = repo['url'] + '/tuf'
    if not real:
        # Add a publisher unknown to the original executable/offline registry.
        name, component = 'community', 'org.community.example'
        registry['repositories'][name] = {
            'url': base + '/' + name, 'components': [component], 'minimum_revision': 1,
            'public_key': keygen(home / 'keys/community.pem'),
            'tuf': demo_trust(home / 'keys/community-tuf', 'community-' + target, base + '/community/tuf')}
        registry['repository_order'].append(name)
        registry['profiles']['community'] = [component]
        payload = home / 'community-payload'
        payload.mkdir()
        (payload / 'community.txt').write_text('Dynamically discovered publisher\n')
        spec = home / 'community.json'
        atomic_json(spec, {'publisher': name, 'target': target, 'season': source['season'], 'channel': source['channel'],
                          'revision': 1, 'packages': [{'id': component, 'name': 'Community example', 'description': 'Test only',
                          'version': '1.0.0', 'dependencies': {}, 'payload': 'community-payload'}]})
        publish(spec, home / 'store', home / 'keys/community.pem', registry, ifw)
    # Expired pinned/intermediate roots, a 2-of-3 key rotation at 257, and a
    # current final root. Archive authorization remains the original independent key.
    trust, _, keys = generate(home / 'long-history', roots)
    old_id = source['tuf']['repository_id']
    trust.update(repository_id=old_id, url=base + '/registry/tuf')
    source['tuf'] = trust
    source['url'] = base + '/registry/registry.json'
    source['allow_loopback_http'] = True
    shutil.rmtree(origin / 'registry')
    shutil.copytree(home / 'long-history/origin', origin / 'registry')
    keygen(home / 'keys/renewed-archive.pem')
    publish_registry(registry, source, home / 'keys/renewed-archive.pem', 2, origin / 'registry', tuf_keys=keys)
    atomic_json(work / 'online-source.json', source)
    atomic_json(work / 'online-registry.json', registry)
    atomic_json(work / 'online-fixture.json', {'home': str(home), 'port': port, 'roots': roots,
                'keys': {role: [str(p) for p in paths] for role, paths in keys.items()}, 'real': real})
    return work


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--work', type=Path, required=True)
    p.add_argument('--ifw', type=Path, required=True)
    p.add_argument('--real', action='store_true')
    p.add_argument('--roots', type=int, default=1030)
    a = p.parse_args()
    online(a.work, a.ifw, a.real, roots=a.roots)
