import argparse,hashlib,json,platform,shutil,subprocess,sys
from pathlib import Path
from datetime import datetime,timedelta,timezone
sys.path.insert(0,str(Path(__file__).resolve().parents[3]))
from tools.check_native_tuf import generate
from installer_tooling.tuf_repository import ROLES,DirectoryStore,publish_catalog,create_root,signer
from installer_tooling.common import canonical,atomic_json
from tuf.api.metadata import Metadata
parser=argparse.ArgumentParser(description='Scoped Native AOT TUF investigation; no production manager changes.')
parser.add_argument('--sigstore',type=Path,required=True)
parser.add_argument('--baronfel',type=Path,required=True)
parser.add_argument('--work',type=Path,required=True)
parser.add_argument('--report',type=Path,required=True)
args=parser.parse_args()
base=args.work.resolve()
if base.exists():raise ValueError('Use a new probe workspace')
base.mkdir(parents=True)
fixtures=base/'fixtures';fixtures.mkdir()
for name,count in [('normal',1),('long',1030)]:generate(fixtures/name,count)
work=base/'checks';work.mkdir()
binaries={'sigstore':args.sigstore.resolve(),'baronfel':args.baronfel.resolve()}
checks=[]
def call(root,origin,state,limit=2048,mode='live',client='sigstore'):
 cmd=[str(binaries[client]),str(root),str(origin),str(state),str(limit),mode]
 p=subprocess.run(cmd,capture_output=True,text=True,timeout=90)
 assert p.returncode in (0,1),(p.returncode,p.stderr)
 r=json.loads(p.stdout);assert (r['status']=='ok')==(p.returncode==0)
 return r

def case(name,expected,fn):
 r=fn();assert r['status']==expected,(name,r)
 checks.append({'name':name,'expected_status':expected,'result':r})
 print(name,r['status'],flush=True)

def setup(name,source='normal'):
 d=work/name;shutil.copytree(fixtures/source,d);return d,d/'root.json',d/'origin/tuf',d/'state'

normalkeys={role:[fixtures/'normal/old'/(role+'.pem')] for role in ROLES};normalkeys['root']=[fixtures/'normal/old'/f'root-{n}.pem' for n in range(3)]
case('python-catalog', 'ok',lambda:call(fixtures/'normal/root.json',fixtures/'normal/origin/tuf',work/'normal-state'))
case('expired-bootstrap-and-1030-roots','ok',lambda:call(fixtures/'long/root.json',fixtures/'long/origin/tuf',work/'long-state'))
case('default-1024-budget-before-current-root','error',lambda:call(fixtures/'long/root.json',fixtures/'long/origin/tuf',work/'bounded-state',1024))
case('next-process-resumes-library-trusted-cache','ok',lambda:call(fixtures/'long/root.json',fixtures/'long/origin/tuf',work/'bounded-state',1024))

def offline():
 d,r,o,s=setup('cache');assert call(r,o,s)['status']=='ok';shutil.rmtree(d/'origin');return call(r,o,s,mode='cache')
case('current-clock-cache-replay-origin-removed','ok',offline)
for role in ['timestamp','snapshot','targets','catalog']:
 def tamper(role=role):
  d,r,o,s=setup('tamper-'+role);assert call(r,o,s)['status']=='ok';shutil.rmtree(d/'origin');shutil.rmtree(s/'metadata')
  pattern={'timestamp':'timestamp.json','snapshot':'*.snapshot.json','targets':'*.targets.json','catalog':'*.catalog.json'}[role]
  p=next((s/'responses').rglob(pattern));data=json.loads(p.read_bytes())
  if role=='catalog':data['revision']=999
  else:data['signed']['version']=999
  p.write_bytes(canonical(data));return call(r,o,s,mode='cache')
 case('reject-tampered-response-'+role,'error',tamper)

def rollback():
 d,r,o,s=setup('rollback');assert call(r,o,s)['status']=='ok';old=(o/'metadata/timestamp.json').read_bytes()
 trust={'repository_id':'evaluation','url':o.as_uri(),'root':json.loads(r.read_bytes())}
 publish_catalog(DirectoryStore(d/'origin'),trust,normalkeys,{'revision':2})
 assert call(r,o,s)['catalog']['revision']==2
 (o/'metadata/timestamp.json').write_bytes(old);return call(r,o,s)
case('timestamp-rollback-across-processes','error',rollback)

def expiry():
 d,r,o,s=setup('expired');assert call(r,o,s)['status']=='ok'
 p=o/'metadata/timestamp.json';m=Metadata.from_bytes(p.read_bytes());m.signed.expires=datetime.now(timezone.utc)-timedelta(days=1);m.signed.version+=1;m.sign(signer(normalkeys['timestamp'][0]));p.write_bytes(m.to_bytes())
 # Store the expired-but-validly-signed response, then replay with the origin gone.
 (s/'responses/metadata/timestamp.json').write_bytes(p.read_bytes());shutil.rmtree(d/'origin');return call(r,o,s,mode='cache')
case('reject-expired-signed-cache-timestamp','error',expiry)
for mutation in ['missing','bad-signature','skip-version']:
 def roots(mutation=mutation):
  d,r,o,s=setup('root-'+mutation,'long');p=o/'metadata/257.root.json'
  if mutation=='missing':p.unlink()
  else:
   v=json.loads(p.read_bytes())
   if mutation=='bad-signature':v['signatures']=[]
   else:v['signed']['version']=258
   p.write_bytes(canonical(v))
  return call(r,o,s)
 case('reject-intermediate-root-'+mutation,'error',roots)
# Characterize the library trust boundary, not a claimed vulnerability: local
# cache is library-trusted; our production policy additionally needs pin replay.
def replaced_root():
 d,r,o,s=setup('replaced-root');assert call(r,o,s)['status']=='ok'
 foreign=work/'foreign';generate(foreign,1)
 cached=s/'metadata'/('metadata-'+hashlib.sha256(b'metadata\0root').hexdigest()+'.json')
 assert cached.exists();cached.write_bytes((foreign/'root.json').read_bytes())
 return call(r,foreign/'origin/tuf',s)
case('characterization-cache-root-is-trusted-without-bootstrap-replay','ok',replaced_root)
# Fully fresh intermediate roots expose the distinction between a library cap
# and our policy of returning pending before accepting any catalog.
def limit_catalog():
 d=work/'fresh-chain';roots={};previous=None
 for i in range(1,6):
  previous=create_root(normalkeys,version=i,previous=previous,root_threshold=2);roots[f'tuf/metadata/{i}.root.json']=canonical(previous)
 store=DirectoryStore(d/'origin');store.publish_immutable(roots);r=d/'root.json';r.write_bytes(roots['tuf/metadata/1.root.json'])
 trust={'repository_id':'evaluation','url':(d/'origin/tuf').as_uri(),'root':json.loads(r.read_bytes())};publish_catalog(store,trust,normalkeys,{'revision':1},initialize=True)
 return call(r,d/'origin/tuf',d/'state',2)
case('characterization-fresh-root-budget-can-still-return-catalog','ok',limit_catalog)
for name,source in [('normal','normal'),('expired-bootstrap','long')]:
 case('baronfel-'+name,'error',lambda source=source:call(fixtures/source/'root.json',fixtures/source/'origin/tuf',work/('baronfel-'+source),client='baronfel'))
report={'schema':1,'date':datetime.now(timezone.utc).isoformat(),'host':platform.platform(),'dotnet_sdk':subprocess.check_output(['dotnet','--version'],text=True).strip(),'target_framework':'net10.0','runtime_identifier':'linux-x64','scope':'Native AOT library investigation using directory-backed transports; no installer port, native archival verifier, HTTP, GUI, clean-OS or Windows/macOS acceptance. Characterization cases document application-policy gaps, not unexpected library test failures.','libraries':{'Tuf':{'package':'Tuf','version':'1.1.1','source_commit':'c0f5ff9747aa9b00c45a964dca275be4066cd489'},'baronfel/tuf-dotnet':{'source_commit':'99ae89aac6e3986e892e8e918d8b02488746bfdf','package':'source reference; not NuGet Tuf'}},'checks':checks,'binaries':{}}
for c in ['sigstore','baronfel']:
 p=binaries[c];report['binaries'][c]={'bytes':p.stat().st_size,'sha256':hashlib.sha256(p.read_bytes()).hexdigest(),'publish_files':[{ 'name':f.name,'bytes':f.stat().st_size} for f in sorted(p.parent.iterdir()) if f.is_file()]}
args.report.parent.mkdir(parents=True,exist_ok=True)
args.report.write_text(json.dumps(report,indent=2)+'\n')
print('Recorded',len(checks),'observations')
