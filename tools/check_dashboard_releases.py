"""Build and check real pinned dashboard release pairs using the native offline gate."""
import argparse
import json
from pathlib import Path
import shutil

from installer_prototype.archival import INDEFINITE_POLICY
from installer_prototype.bundle_publication import paths, prepare_bundle, render_page
from installer_prototype.common import atomic_json, keygen, version
from installer_prototype.dashboard_packages import package_release
from installer_prototype.offline_acceptance import check_candidate, host_target
from installer_prototype.publication_storage import Object
from installer_prototype.registry_updates import publish_registry
from installer_prototype.repository import publish
from installer_prototype.tuf_repository import demo_trust


def check_releases(lock_path, ifw, work, cache, report, *, prepare_only=False):
    work = Path(work).resolve()
    if work.exists():
        raise ValueError('Real release workspace must be new')
    lock = json.loads(Path(lock_path).read_text())
    for definition in lock['publishers'].values():
        releases = definition['releases']
        if len(releases) != 2 or version(releases[0]['version']) >= version(releases[1]['version']):
            raise ValueError('Acceptance lock must specify exactly two increasing versions per publisher')
    target = host_target()
    keys, store = work / 'keys', work / 'store'
    registry = {'schema': 1, 'target': target, 'season': '2026', 'channel': 'real-packages',
                'online_trust': 'tuf', 'archive_policy': dict(INDEFINITE_POLICY), 'repositories': {},
                'profiles': {}, 'repository_order': list(lock['publishers'])}
    for publisher, definition in lock['publishers'].items():
        url = (store / target / publisher).as_uri()
        registry['repositories'][publisher] = {
            'url': url, 'components': [definition['component']], 'public_key': keygen(keys / f'{publisher}.pem'),
            'tuf': demo_trust(keys / (publisher + '-tuf'), f'real-{target}-{publisher}', url + '/tuf')}
        registry['profiles'][publisher] = [definition['component']]
    registry['profiles']['tools'] = [c['component'] for c in lock['publishers'].values()]
    registry['bundle_public_key'] = keygen(keys / 'bundle.pem')
    source = {'kind': 'registry-source', 'schema': 1, 'registry_id': f'real-dashboard-check-{target}',
              'target': target, 'season': registry['season'], 'channel': registry['channel'],
              'minimum_revision': 1, 'archive_policy': dict(INDEFINITE_POLICY),
              'public_key': keygen(keys / 'registry.pem'), 'url': (store / target / 'registry/registry.json').as_uri()}
    source['tuf'] = demo_trust(keys / 'registry-tuf', f'real-{target}-registry', (store / target / 'registry/tuf').as_uri())
    atomic_json(work / 'registry-source.json', source)
    publish_registry(registry, source, keys / 'registry.pem', 1, store / target / 'registry')
    public = work / 'public'
    public.mkdir(parents=True)

    class Origin:
        def get(self, name):
            path = public / name
            return Object(path.read_bytes(), 'local-test-origin') if path.exists() else None

    provenance = {'target': target, 'publishers': {}}
    for revision in (0, 1):
        for publisher, definition in lock['publishers'].items():
            release = definition['releases'][revision]
            print(f"Packaging {publisher} {release['tag']} for {target}", flush=True)
            output = work / 'packages' / f'{publisher}-{revision}'
            spec = package_release(lock_path, publisher, target, release['tag'], output, cache)
            publish(spec, store, keys / f'{publisher}.pem', registry, ifw)
            provenance['publishers'].setdefault(publisher, []).append(release)
            shutil.rmtree(output)  # Immutable repository now owns the verified bytes.
        destination = work / ('baseline' if revision == 0 else 'candidate')
        plan = prepare_bundle(source, Origin(), keys / 'bundle.pem', work / 'cache', destination,
                              public.as_uri(), initialize=revision == 0)
        if revision == 0:
            record = plan['current']
            publication = record['publication']['signed']
            dest = public / paths(publication)['bundle']
            dest.parent.mkdir(parents=True)
            shutil.copyfile(destination / 'current' / (publication['id'] + '.zip'), dest)
            (public / 'index.html').write_bytes(render_page({'current': record}))
    atomic_json(work / 'provenance.json', provenance)
    if prepare_only:
        return provenance
    print('Running native offline fresh/profile/upgrade/downgrade checks', flush=True)
    result = check_candidate(work / 'candidate', work / 'registry-source.json', ifw, work / 'native', report)
    expected = {d['component']: d['releases'][1]['version'] for d in lock['publishers'].values()}
    if result['inventory'] != expected or len(result['upstream_releases']) != len(expected):
        raise ValueError('Real package acceptance did not install the pinned current releases')
    print(json.dumps(result, indent=2), flush=True)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--lock', type=Path, default=Path('packaging/upstream-lock.json'))
    parser.add_argument('--ifw', type=Path, required=True)
    parser.add_argument('--work', type=Path, default=Path('build/dashboard-releases'))
    parser.add_argument('--cache', type=Path, default=Path('build/upstream-cache'))
    parser.add_argument('--report', type=Path, default=Path('build/dashboard-report.json'))
    parser.add_argument('--prepare-only', action='store_true')
    args = parser.parse_args()
    check_releases(args.lock, args.ifw, args.work, args.cache, args.report, prepare_only=args.prepare_only)


if __name__ == '__main__':
    main()
