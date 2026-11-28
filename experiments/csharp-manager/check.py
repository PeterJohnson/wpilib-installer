"""Reuse the existing native client policy acceptance cases, without changing their assertions."""
import argparse,json,platform,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from tools.check_native_tuf import Evaluation,generate
from installer_tooling.common import atomic_json,digest
p=argparse.ArgumentParser(description=__doc__)
p.add_argument('--app',type=Path,required=True);p.add_argument('--work',type=Path,required=True);p.add_argument('--report',type=Path,required=True);p.add_argument('--registry-source',type=Path)
a=p.parse_args();work=a.work.resolve();work.mkdir(parents=True,exist_ok=False)
normal=work/'normal';_,_,normal_keys=generate(normal,1)
long=work/'long';_,_,long_keys=generate(long,1030)
e=Evaluation('csharp-tuf',a.app,work/'cases',normal,long,a.registry_source);e.work.mkdir()
e.normal_cases(normal_keys);e.long_cases(long_keys);e.archival_cases(long_keys);e.native_archival_cases()
from extra_checks import run,existing
existing(e)
run(e,normal_keys)
report={'schema':1,'host':platform.platform(),'library':'Tuf 1.1.1','scope':'Native AOT manager policy adapter; same Python-generated fixtures/assertions as Go/Rust, with separate archival authority; no complete installer migration','binary_bytes':a.app.stat().st_size,'binary_sha256':digest(a.app),'checks':e.results,'status':'passed' if all(c['status']=='passed' for c in e.results) else 'failed'}
atomic_json(a.report,report);atomic_json(work/'invocations.json',e.invocations)
print(json.dumps(report,indent=2));sys.exit(report['status']!='passed')
