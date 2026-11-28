"""Manager-specific gaps identified by the initial C# library investigation."""
import copy,functools,json,shutil,subprocess,threading
from http.server import SimpleHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
from installer_tooling.common import atomic_json,canonical,keygen,sign
from installer_tooling.tuf_repository import DirectoryStore,ROLES,create_root,publish_catalog
from tools.check_native_tuf import generate
from tests.test_archival import IndefiniteArchivalTests

def run(e,keys):
 def fresh_budget():
  d=e.work/'fresh-budget';store=DirectoryStore(d/'origin');roots={};previous=None
  for i in range(1,6):
   previous=create_root(keys,version=i,previous=previous,root_threshold=2);roots[f'tuf/metadata/{i}.root.json']=canonical(previous)
  store.publish_immutable(roots);r=d/'root.json';r.write_bytes(roots['tuf/metadata/1.root.json']);o=d/'origin/tuf';s=d/'state'
  publish_catalog(store,{'repository_id':'evaluation','root':json.loads(r.read_bytes()),'url':o.as_uri()},keys,{'revision':1},initialize=True)
  for _ in range(2):
   result=e.reject(r,o,s,budget=2);assert 'root-update-pending' in result['error'];assert not (s/'responses/metadata/timestamp.json').exists()
  assert e.ok(r,o,s,budget=2)['root_version']==5
 e.case('fresh-root-budget-never-accepts-catalog-early',fresh_budget)
 def replacement():
  d,r,o,s=e.fixture('replacement');e.ok(r,o,s)
  foreign=e.work/'foreign';generate(foreign,1)
  (s/'metadata').mkdir(exist_ok=True);shutil.copy2(foreign/'root.json',s/'metadata/root.json')
  e.reject(r,foreign/'origin/tuf',s)
 e.case('library-cache-root-substitution-cannot-replace-bootstrap',replacement)
 def missing_history():
  d,r,o,s=e.fixture('missing-cache-history',long=True);e.reject(r,o,s,budget=256)
  (s/'responses/metadata/120.root.json').unlink();e.reject(r,o,s,budget=256)
 e.case('missing-cache-root-below-checkpoint-rejected',missing_history)
 def write_failure():
  d,r,o,s=e.fixture('write-failure');s.mkdir();(s/'metadata').write_text('not a directory')
  e.reject(r,o,s)
 e.case('cache-write-failure-is-fatal',write_failure)
 def http_cases():
  d,r,o,s=e.fixture('http');behavior={'status':None}
  class Handler(SimpleHTTPRequestHandler):
   def log_message(self,*args):pass
   def do_GET(self):
    if self.path.endswith('timestamp.json') and behavior['status']:
     self.send_response(behavior['status'])
     if behavior['status']==302:self.send_header('Location','https://example.invalid/redirect')
     if behavior['status']==200:self.send_header('Content-Length',str(9*1024*1024))
     self.end_headers();return
    super().do_GET()
  server=ThreadingHTTPServer(('127.0.0.1',0),functools.partial(Handler,directory=str(o)))
  thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
  url=f'http://127.0.0.1:{server.server_port}'
  try:
   assert e.ok(r,url,s)['catalog']['revision']==1
   for status in (302,500,200):behavior['status']=status;e.reject(r,url,s)
  finally:server.shutdown();server.server_close();thread.join()
  assert e.ok(r,url,s,mode='cache')['catalog']['revision']==1
 e.case('http-refresh-no-redirect-no-error-fallback-bounded-length-explicit-cache',http_cases)
 def registry():
  f=IndefiniteArchivalTests();f.setUp()
  try:
   state=e.work/'registry-cache';source=e.work/'registry-source.json';atomic_json(source,f.source)
   r=copy.deepcopy(f.record['signed']);r['registry']['repositories']={}
   registrykeys={role:[f.root/'registry-tuf'/(role+'.pem')] for role in ROLES}
   def publisher(name):
    trust,_,k=generate(f.root/name,1)
    trust['repository_id']=name
    catalog={'schema':1,'kind':'publisher-catalog','publisher':name,'season':'2027','channel':'stable','target':'linux-x64','releases':[]}
    publish_catalog(DirectoryStore(f.root/name/'origin'),trust,k,catalog)
    r['registry']['repositories'][name]={'tuf':trust}
   def publish():publish_catalog(DirectoryStore(f.root/'feed'),f.source['tuf'],registrykeys,sign(r,f.authority))
   def call(expected='ok',mode='live'):
    p=subprocess.run([str(e.binary),'registry',str(source),str(state),mode],capture_output=True,text=True,timeout=90)
    result=json.loads(p.stdout);assert result['status']==expected,(p.returncode,result);return result
   publisher('elastic');publish();assert set(call()['publishers'])=={'elastic'}
   publisher('robotpy');r['revision']=2;publish();assert set(call()['publishers'])=={'elastic','robotpy'}
   # Rotation of archival authority does not affect online authentication.
   f.authority=f.root/'new-archive.pem';keygen(f.authority);r['revision']=3;publish();assert call()['registry_revision']==3
   r['revision']=2;publish();call('error')
   r['revision']=4;publish();call()
   for name in ('feed','elastic','robotpy'):shutil.rmtree(f.root/name)
   assert set(call(mode='cache')['publishers'])=={'elastic','robotpy'}
  finally:f.doCleanups()
 e.case('authenticated-registry-dynamic-publishers-archival-key-rotation-revision-rollback-cache',registry)
 def canonical_order():
  f=IndefiniteArchivalTests();f.setUp()
  try:
   record=copy.deepcopy(f.record['signed']);record['description']={'\ue000':'BMP','\U00010000':'supplementary','n':18446744073709551615}
   envelope=sign(record,f.authority);atomic_json(f.bundle/'registry.json',envelope)
   from installer_tooling.registry_updates import registry_identity
   manifest=copy.deepcopy(f.manifest);manifest['registry']=registry_identity(envelope);atomic_json(f.bundle/'bundle-manifest.json',sign(manifest,f.bundle_key))
   source=e.work/'unicode-order.json';atomic_json(source,f.source)
   e.ok(source,f.bundle,e.work/'no-online-state',mode='archive')
  finally:f.doCleanups()
 e.case('archival-python-unicode-scalar-order-and-uint64',canonical_order)
