"""Cross-language evaluation against the existing Python publisher and archival policy.

Probe transports use local repository bytes: no live production publication or network.
Native clients perform TUF and schema-3 indefinite archival authorization checks.
The Python archival implementation is the oracle. Full bundle payload validation,
legacy archival schemas and the production manager are outside this probe.
"""
import argparse
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
from urllib.parse import urlparse, unquote
from unittest.mock import patch

from installer_tooling.common import atomic_json, canonical, digest, keygen, sign
from installer_tooling.tuf_repository import (DirectoryStore, ROLES, create_root,
    encoded, fetch_catalog, publish_catalog, signer)
from tuf.api.metadata import Metadata


def generate(root, count):
    keys = []
    for group in ('old', 'new'):
        k = {role: [root / group / (role + '.pem')] for role in ROLES}
        k['root'] = [root / group / f'root-{n}.pem' for n in range(3)]
        for paths in k.values():
            for path in paths:
                keygen(path)
        keys.append(k)
    previous = None
    files = {}
    for version in range(1, count + 1):
        k = keys[int(version >= 257)]
        if version == 257:
            k = {**k, 'old_root': keys[0]['root']}
        with patch('installer_tooling.tuf_repository.datetime') as clock:
            clock.now.return_value = datetime.now(timezone.utc) if version == count else datetime(2018, 1, 1, tzinfo=timezone.utc)
            previous = create_root(k, version=version, previous=previous, root_threshold=2)
        files[f'tuf/metadata/{version}.root.json'] = canonical(previous)
    bootstrap = json.loads(files['tuf/metadata/1.root.json'])
    store = DirectoryStore(root / 'origin')
    store.publish_immutable(files)
    trust = {'repository_id': 'evaluation', 'url': (root / 'origin/tuf').as_uri(), 'root': bootstrap}
    atomic_json(root / 'root.json', bootstrap)
    active_keys = keys[int(count >= 257)]
    publish_catalog(store, trust, active_keys, {'revision': 1, 'unicode': 'Robôt 🤖 <>&'}, initialize=True)
    return trust, keys, active_keys


class Evaluation:
    def __init__(self, client, binary, work, normal, long, existing=None):
        self.client, self.binary, self.work = client, Path(binary).resolve(), work
        self.normal, self.long, self.existing = normal, long, existing
        self.results, self.invocations = [], []

    def invoke(self, root, origin, state, *, mode='live', limit=2048, budget=0):
        if budget:
            # Size replay work from filenames only; the native library still
            # authenticates every cached root from the original pin.
            version=json.loads(Path(root).read_text())['signed']['version']
            cached=0
            while (Path(state)/'responses/metadata'/f'{version+cached+1}.root.json').is_file(): cached+=1
            limit=cached+budget+2
        p = subprocess.run([str(self.binary), str(root), str(origin), str(state), mode, str(limit), str(budget)], capture_output=True, text=True, timeout=90)
        try:
            result = json.loads(p.stdout)
        except Exception as exc:
            raise AssertionError(f'Probe protocol failure: {p.returncode}, {p.stdout[-500:]}, {p.stderr[-500:]}') from exc
        assert p.returncode in (0, 1), (p.returncode, result, p.stderr[-4000:])
        assert (p.returncode == 0) == (result['status'] == 'ok'), result
        self.invocations.append({'mode': mode, 'limit': limit, 'budget': budget, **result})
        return result

    def ok(self, root, origin, state, **kwargs):
        r = self.invoke(root, origin, state, **kwargs)
        assert r['status'] == 'ok', r
        return r

    def reject(self, root, origin, state, **kwargs):
        r = self.invoke(root, origin, state, **kwargs)
        assert r['status'] == 'error', r
        assert 'catalog' not in r, r
        return r

    def fixture(self, name, *, long=False):
        directory = self.work / name
        source = self.long if long else self.normal
        shutil.copytree(source / 'origin', directory / 'origin')
        shutil.copyfile(source / 'root.json', directory / 'root.json')
        return directory, directory / 'root.json', directory / 'origin/tuf', directory / 'state'

    def case(self, name, fn):
        try:
            detail = fn()
            self.results.append({'name': name, 'status': 'passed', 'detail': detail})
        except Exception as exc:
            self.results.append({'name': name, 'status': 'failed', 'error': str(exc)})
        print(self.client, name, self.results[-1]['status'], flush=True)

    def normal_cases(self, active_keys):
        def roundtrip():
            d, r, o, s = self.fixture('roundtrip')
            first = self.ok(r, o, s)
            assert first['catalog'] == {'revision': 1, 'unicode': 'Robôt 🤖 <>&'}
            trust = {'repository_id': 'evaluation', 'url': o.as_uri(), 'root': json.loads(r.read_text())}
            old = (o / 'metadata/timestamp.json').read_bytes()
            publish_catalog(DirectoryStore(d/'origin'), trust, active_keys, {'revision': 2})
            assert self.ok(r, o, s)['catalog'] == {'revision': 2}
            (o / 'metadata/timestamp.json').write_bytes(old)
            self.reject(r, o, s)
        self.case('python-publisher-update-and-rollback', roundtrip)

        def cache():
            d, r, o, s = self.fixture('cache')
            first = self.ok(r, o, s)
            shutil.rmtree(d/'origin')
            cached = self.ok(r, o, s, mode='cache')
            assert cached['catalog'] == first['catalog']
        self.case('verified-cache-with-origin-removed', cache)

        for what in ('timestamp', 'snapshot', 'targets', 'catalog'):
            def tamper(what=what):
                d, r, o, s = self.fixture('cache-tamper-'+what)
                self.ok(r, o, s)
                pattern = {'timestamp': 'timestamp.json', 'snapshot': '*.snapshot.json', 'targets': '*.targets.json', 'catalog': '*.catalog.json'}[what]
                path = next((s/'responses').rglob(pattern))
                data = path.read_bytes()
                path.write_bytes(data.replace(b'"revision":1', b'"revision":9') if what == 'catalog' else data.replace(b'"version":1', b'"version":9'))
                assert path.read_bytes() != data
                # A valid library copy is a legitimate fallback; corrupt all
                # copies to test rejection when no valid metadata remains.
                if what in ('timestamp','snapshot','targets'):
                    for local in (s/'metadata').rglob(what+'.json'):
                        original=local.read_bytes()
                        damaged=json.loads(original)
                        damaged['signed']['version']=9
                        atomic_json(local,damaged)
                shutil.rmtree(d/'origin')
                self.reject(r, o, s, mode='cache')
            self.case('cached-'+what+'-tampering', tamper)

        def expired_cache():
            d,r,o,s = self.fixture('expired-cache')
            self.ok(r,o,s)
            trust={'repository_id':'evaluation','url':o.as_uri(),'root':json.loads(r.read_text())}
            publish_catalog(DirectoryStore(d/'origin'),trust,active_keys,now=datetime.now(timezone.utc)-timedelta(days=3))
            # Authentic expired data, as if saved earlier; never alter client clock.
            response=s/'responses'
            shutil.rmtree(response)
            shutil.copytree(o,response)
            shutil.rmtree(s/'metadata')
            shutil.rmtree(d/'origin')
            self.reject(r,o,s,mode='cache')
        self.case('authentic-expired-cache-rejected',expired_cache)

        def wrong_root():
            d,r,o,s=self.fixture('wrong-root')
            other=self.work/'other-authority'
            generate(other,1)
            self.reject(other/'root.json',o,s)
        self.case('independent-publisher-root-substitution',wrong_root)

        for what in ('timestamp','snapshot','catalog'):
            def tamper(what=what):
                d,r,o,s=self.fixture('origin-tamper-'+what)
                pattern={'timestamp':'timestamp.json','snapshot':'*.snapshot.json','catalog':'*.catalog.json'}[what]
                p=next(o.rglob(pattern));p.write_bytes(b'{}')
                self.reject(r,o,s)
            self.case('origin-'+what+'-tampering',tamper)

    def long_cases(self, keys):
        def default_limit():
            d,r,o,s=self.fixture('default-limit',long=True)
            result=self.reject(r,o,s,limit=0)
            return {'expected_limitation':result['error']}
        self.case('default-limit-cannot-cross-1030-roots',default_limit)

        def complete():
            d,r,o,s=self.fixture('long-complete',long=True)
            result=self.ok(r,o,s)
            assert result['root_version']==1030
            shutil.rmtree(d/'origin')
            assert self.ok(r,o,s,mode='cache')['root_version']==1030
        self.case('expired-bootstrap-and-1030-root-history',complete)

        def resume():
            d,r,o,s=self.fixture('long-resume',long=True)
            for attempt in range(4):
                result=self.reject(r,o,s,budget=256)
                assert 'root-update-pending' in result['error'],result
                assert not (s/'responses/metadata/timestamp.json').exists()
                assert len(list((s/'responses/metadata').glob('*.root.json')))==256*(attempt+1)
            result=self.ok(r,o,s,budget=256)
            assert result['root_version']==1030 and result['new_roots']==5,result
            return {'pending_invocations':4,'final_root':1030,'last_downloaded_roots':5}
        self.case('bounded-catch-up-resumes-without-catalog-at-boundary',resume)
        def corrupt_resume():
            d,r,o,s=self.fixture('resume-tamper',long=True)
            self.reject(r,o,s,budget=256)
            path=s/'responses/metadata/257.root.json'
            data=json.loads(path.read_bytes());data['signed']['expires']='2999-01-01T00:00:00Z';atomic_json(path,data)
            result=self.reject(r,o,s,budget=256)
            assert 'root-update-pending' not in result['error'],result
        self.case('cached-root-reverified-on-resume',corrupt_resume)

        for side in ('old','new'):
            def threshold(side=side):
                d,r,o,s=self.fixture('threshold-'+side,long=True)
                p=o/'metadata/257.root.json';m=Metadata.from_file(str(p))
                trusted=Metadata.from_file(str(r)) if side=='old' else m
                ids=trusted.signed.roles['root'].keyids
                m.signatures={k:v for k,v in m.signatures.items() if k not in ids[1:]}
                p.write_bytes(encoded(m));self.reject(r,o,s)
            self.case('rotation-requires-'+side+'-threshold',threshold)

        for mutation in ('missing','skipped','expired-final'):
            def broken(mutation=mutation):
                d,r,o,s=self.fixture('broken-'+mutation,long=True)
                p=o/'metadata/300.root.json'
                if mutation=='missing': p.unlink()
                elif mutation=='skipped': p.write_bytes((o/'metadata/301.root.json').read_bytes())
                else:
                    p=o/'metadata/1030.root.json';m=Metadata.from_file(str(p));m.signed.expires=datetime.now(timezone.utc)-timedelta(days=1)
                    for key in keys['root']:m.sign(signer(key),append=True)
                    p.write_bytes(encoded(m))
                self.reject(r,o,s)
            self.case('root-history-'+mutation,broken)

    def archival_cases(self, long_keys):
        from tests.test_archival import IndefiniteArchivalTests
        from installer_tooling.registry_updates import resolve, validated_tuf_record, verified_record
        from installer_tooling.archival import verify_archival
        from installer_tooling.ci_publication import tuf_registry_release
        from installer_tooling.tuf_repository import Fetcher

        def boundary():
            f=IndefiniteArchivalTests();f.setUp()
            try:
                source=f.source;root=self.work/'archive-root.json';atomic_json(root,source['tuf']['root'])
                state=self.work/'archive-state';origin=f.root/'feed/tuf'
                source_file=self.work/'archive-source.json';atomic_json(source_file,source)
                archive_state=self.work/'native-archive-state'
                assert self.ok(source_file,f.bundle,archive_state,mode='archive')['registry_revision']==1
                assert not archive_state.exists()
                assert validated_tuf_record(self.ok(root,origin,state)['catalog'],source)==f.registry
                key=f.root/'replacement-archive.pem';new_public=keygen(key)
                keys={role:[f.root/'registry-tuf'/(role+'.pem')] for role in ROLES}
                tuf_registry_release(source,DirectoryStore(f.root/'feed'),key,keys)
                online=self.ok(root,origin,state)['catalog']
                assert online['signed']['revision']==2
                assert validated_tuf_record(online,source)==f.registry
                try: verified_record(online,source,mode='offline')
                except ValueError: pass
                else: raise AssertionError('new archival signature accepted with old pin')
                assert verified_record(online,{**source,'public_key':new_public},mode='offline')==f.registry
                before={p.relative_to(state).as_posix():p.read_bytes() for p in state.rglob('*') if p.is_file()}
                # Existing archival implementation deliberately has no native-TUF call.
                with patch.object(Fetcher,'_fetch',side_effect=AssertionError('archival network access')), patch('installer_tooling.archival.time.time',return_value=4102444800):
                    view=resolve(source,self.work/'archive-offline-cache',bundle=f.bundle)
                    assert view.record==f.record
                    assert self.ok(source_file,f.bundle,archive_state,mode='archive')['registry_revision']==1
                    verify_archival(f.manifest,view.registry,view.record)
                assert before=={p.relative_to(state).as_posix():p.read_bytes() for p in state.rglob('*') if p.is_file()}
                shutil.rmtree(f.root/'feed')
                assert self.ok(root,origin,state,mode='cache')['catalog']==online
                return {'native':'online TUF/cache and independent archival authorization','python_oracle':'archival authorization at year 2100; old offline pin retained','online_revision':2,'offline_revision':1}
            finally:f.doCleanups()
        self.case('archival-key-rotation-old-bundle-and-online-cache',boundary)

        def forged():
            f=IndefiniteArchivalTests();f.setUp()
            try:
                root=self.work/'forge-root.json';atomic_json(root,f.source['tuf']['root'])
                path=next((f.root/'feed/tuf/targets').glob('*.catalog.json'))
                record=copy.deepcopy(f.record['signed']);record['revision']=999
                path.write_bytes(canonical(sign(record,f.authority)))
                self.reject(root,f.root/'feed/tuf',self.work/'forge-state')
            finally:f.doCleanups()
        self.case('archival-signer-cannot-authorize-online-catalog',forged)

        def old_media_to_online():
            from installer_tooling.registry_updates import registry_identity
            f=IndefiniteArchivalTests();f.setUp()
            try:
                d,r,o,state=self.fixture('archival-to-long-online',long=True)
                source=copy.deepcopy(f.source)
                source['tuf']={'repository_id':'archive-long-authority','root':json.loads(r.read_text()),'url':o.as_uri()}
                old=copy.deepcopy(f.record['signed']);old.update(issued_at=1514764800,expires_at=1546300800)
                old_record=sign(old,f.authority)
                manifest=copy.deepcopy(f.manifest);manifest['registry']=registry_identity(old_record);manifest['archival']['issued_at']=old['issued_at']
                atomic_json(f.bundle/'registry.json',old_record)
                atomic_json(f.bundle/'bundle-manifest.json',sign(manifest,f.bundle_key))
                source_path=d/'source.json';atomic_json(source_path,source)
                assert self.ok(source_path,f.bundle,state,mode='archive')['registry_revision']==1
                assert not state.exists()
                new_key=f.root/'new-archive-authority.pem';keygen(new_key)
                current=copy.deepcopy(f.record['signed']);current['revision']=2
                record=sign(current,new_key)
                publish_catalog(DirectoryStore(d/'origin'),source['tuf'],long_keys,record)
                result=self.ok(r,o,state)
                assert result['root_version']==1030
                assert validated_tuf_record(result['catalog'],source)==f.registry
                assert self.ok(source_path,f.bundle,state,mode='archive')['registry_revision']==1
                return {'old_online_authorization_expired':True,'old_root_expired':True,'current_root':1030,'archival_key_rotated':True}
            finally:f.doCleanups()
        self.case('old-offline-authorization-then-online-through-1030-roots',old_media_to_online)

    def native_archival_cases(self):
        from tests.test_archival import IndefiniteArchivalTests
        from installer_tooling.registry_updates import registry_identity, resolve
        from installer_tooling.archival import verify_archival
        for mutation in ('none','unicode','registry-signature','bundle-signature','wrong-pin','registry-binding','policy','publisher-identity'):
            def test(mutation=mutation):
                f=IndefiniteArchivalTests();f.setUp()
                try:
                    # Archive actually carries already-expired online authorization.
                    signed=f.record['signed']
                    signed.update(issued_at=1514764800,expires_at=1546300800)
                    if mutation=='unicode':
                        signed['description']={'Robôt 🤖':'<>& / \b \f \n \r \t \x00 \x7f café','large':9007199254740993,'list':[True,False,None,-1]}
                    record=sign(signed,f.authority)
                    manifest=copy.deepcopy(f.manifest)
                    manifest['registry']=registry_identity(record)
                    manifest['archival']['issued_at']=signed['issued_at']
                    atomic_json(f.bundle/'registry.json',record)
                    atomic_json(f.bundle/'bundle-manifest.json',sign(manifest,f.bundle_key))
                    source=copy.deepcopy(f.source)
                    if mutation=='registry-signature':
                        record['signed']['revision']=9;atomic_json(f.bundle/'registry.json',record)
                    elif mutation=='bundle-signature':
                        b=json.loads((f.bundle/'bundle-manifest.json').read_text());b['signed']['registry']['revision']=9;atomic_json(f.bundle/'bundle-manifest.json',b)
                    elif mutation=='wrong-pin':source['public_key']=keygen(f.root/'wrong.pem')
                    elif mutation in ('registry-binding','policy','publisher-identity'):
                        if mutation=='registry-binding':manifest['registry']['sha256']='0'*64
                        elif mutation=='policy':manifest['archival']['expires_at']=4102444800
                        else:manifest['archival']['publishers_tuf']['core']['repository_id']='impostor'
                        atomic_json(f.bundle/'bundle-manifest.json',sign(manifest,f.bundle_key))
                    path=self.work/('archive-source-'+mutation+'.json');atomic_json(path,source)
                    state=self.work/('archive-state-'+mutation)
                    if mutation in ('none','unicode'):
                        result=self.ok(path,f.bundle,state,mode='archive')
                        assert result['registry_sha256']==registry_identity(record)['sha256']
                        view=resolve(source,f.root/'cache',bundle=f.bundle)
                        verify_archival(manifest,view.registry,view.record)
                    else:
                        self.reject(path,f.bundle,state,mode='archive')
                        try:
                            view=resolve(source,f.root/'cache',bundle=f.bundle)
                            actual=json.loads((f.bundle/'bundle-manifest.json').read_text())['signed']
                            verify_archival(actual,view.registry,view.record)
                        except ValueError:pass
                        else:raise AssertionError('Python oracle unexpectedly accepted invalid archive')
                    assert not state.exists(),'archive touched online cache'
                finally:f.doCleanups()
            self.case('native-archival-'+mutation,test)

    def existing_cases(self):
        if not self.existing:return
        from installer_tooling.registry_updates import resolve
        source=json.loads(self.existing.read_text())
        view=resolve(source,self.work/'python-existing-cache')
        trusts={'registry':source['tuf'],**{name:repo['tuf'] for name,repo in view.registry['repositories'].items()}}
        for name,trust in trusts.items():
            def test(name=name,trust=trust):
                parsed=urlparse(trust['url']);assert parsed.scheme=='file','optional comparison expects a local repository copy'
                origin=Path(unquote(parsed.path));root=self.work/(name+'-root.json');atomic_json(root,trust['root'])
                native=self.ok(root,origin,self.work/(name+'-state'))['catalog']
                reference=fetch_catalog(trust,self.work/'python-existing-cache')[0]
                assert native==reference
                return {'catalog_sha256':hashlib.sha256(canonical(native)).hexdigest(),'repository_id':trust['repository_id']}
            self.case('existing-repository-'+name,test)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--go',type=Path,required=True)
    p.add_argument('--work',type=Path,required=True);p.add_argument('--report',type=Path,required=True)
    p.add_argument('--registry-source',type=Path)
    args=p.parse_args();work=args.work.resolve()
    if work.exists():raise ValueError('Use a new evaluation workspace')
    work.mkdir(parents=True)
    normal=work/'normal';_,_,normal_keys=generate(normal,1)
    long=work/'long';_,_,long_keys=generate(long,1030)
    report={'schema':1,'host':platform.platform(),'libraries':{'go-tuf':'2.5.0'},
            'scope':'native TUF/local transports, explicit response-cache adapters, native schema-3 indefinite archival authorization against Python oracle; no full bundle loader or HTTP/GUI validation',
            'clients':{}}
    for name,binary in (('go-tuf',args.go),):
        e=Evaluation(name,binary,work/name,normal,long,args.registry_source)
        e.work.mkdir()
        e.normal_cases(normal_keys);e.long_cases(long_keys);e.archival_cases(long_keys);e.native_archival_cases();e.existing_cases()
        report['clients'][name]={'binary_sha256':digest(binary),'binary_bytes':binary.stat().st_size,'checks':e.results}
        atomic_json(work/(name+'-invocations.json'),e.invocations)
    report['status']='passed' if all(c['status']=='passed' for client in report['clients'].values() for c in client['checks']) else 'failed'
    atomic_json(args.report,report)
    print(json.dumps({'status':report['status'],'report':str(args.report)},indent=2))
    return 0 if report['status']=='passed' else 1

if __name__=='__main__':sys.exit(main())
