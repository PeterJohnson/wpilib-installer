"""Build the experimental one-file Avalonia/AOT application with private Qt assets."""
import argparse,hashlib,json,os,platform,shutil,subprocess,sys,zipfile
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'tools'))
from installer_tooling.qt_engine import build_installer
p=argparse.ArgumentParser(description=__doc__)
p.add_argument('--source',type=Path,required=True);p.add_argument('--ifw',type=Path,required=True)
p.add_argument('--work',type=Path,required=True);p.add_argument('--assets',type=Path)
p.add_argument('--baseline',type=Path,required=True)
a=p.parse_args();work=a.work.resolve();work.mkdir(parents=True,exist_ok=True)
root=Path(__file__).resolve().parent
rid=('win-x64' if os.name=='nt' else ('osx-arm64' if platform.machine()=='arm64' else 'osx-x64') if sys.platform=='darwin' else 'linux-x64')
def zip_tree(source,destination):
 with zipfile.ZipFile(destination,'w',zipfile.ZIP_DEFLATED,compresslevel=6) as z:
  for f in sorted(source.rglob('*')):
   if f.is_file() and not (f.suffix in ('.pdb','.dbg') or '.dSYM' in str(f)):z.write(f,f.relative_to(source))
def publish(project,output,*properties):
 subprocess.run(['dotnet','publish',str(root/project),'-c','Release','-r',rid,'-o',str(output),*properties],check=True)
assets=a.assets.resolve() if a.assets else work/'assets'
if not a.assets:
 assets.mkdir(parents=True,exist_ok=True);shutil.copy2(a.source,assets/'bootstrap.json')
 suffix='.exe' if os.name=='nt' else '.app' if sys.platform=='darwin' else ''
 engine=build_installer(a.ifw,assets/('engine'+suffix),maintenance_name='.wpilib-engine')
 with zipfile.ZipFile(assets/'engine.zip','w',zipfile.ZIP_DEFLATED,compresslevel=1) as z:
  for f in sorted(engine.rglob('*')) if engine.is_dir() else [engine]:
   if f.is_file():z.write(f,f.relative_to(assets))
 if sys.platform=='linux':
  # Match Rust's minimal baseline image, including the exact runtime-notices policy.
  image='wpilib-installer-linux-runtime:22.04-rust-1.100.0'
  repo=Path.cwd().resolve()
  subprocess.run(['docker','build','-f','.github/containers/linux-runtime.Dockerfile','-t',image,'.'],check=True)
  subprocess.run(['docker','run','--rm','--user',f'{os.getuid()}:{os.getgid()}',
                  '-v',f'{repo}:/work:ro','-v',f'{assets}:/assets',image,'python3',
                  'tools/package_linux_runtime.py','--engine','/assets/engine','--output','/assets/runtime.zip'],check=True)
publish('app/App.csproj',work/'private',f'-p:ManagerAssets={assets}')
zip_tree(work/'private',work/'application.zip')
publish('launcher/Launcher.csproj',work/'launcher',f'-p:Payload={work / "application.zip"}')
download=work/'download';download.mkdir(exist_ok=True)
name='WPILibManager-CSharp'+('.exe' if os.name=='nt' else '')
if sys.platform=='darwin':
 app=download/'WPILib CSharp Experiment.app';exe=app/'Contents/MacOS'/name;exe.parent.mkdir(parents=True,exist_ok=True)
 import plistlib
 (app/'Contents/Info.plist').write_bytes(plistlib.dumps({'CFBundleName':'WPILib CSharp Experiment','CFBundleExecutable':name,'CFBundleIdentifier':'org.wpilib.csharp-experiment','CFBundlePackageType':'APPL','CFBundleVersion':'0.1','LSMinimumSystemVersion':'13.0'}))
else:exe=download/name
shutil.copy2(work/'launcher'/name,exe)
zip_tree(download,work/'csharp-application.zip')
# Baseline artifact is a ZIP preserving the Rust executable's permissions.
baseline=work/'rust-baseline';baseline.mkdir(exist_ok=True)
with zipfile.ZipFile(a.baseline) as z:z.extractall(baseline)
rust=[f for f in baseline.rglob('*') if f.is_file() and f.name in ('wpilib-manager','WPILibManager.exe')]
assert len(rust)==1,rust
native=[{'name':f.relative_to(work/'private').as_posix(),'bytes':f.stat().st_size,'sha256':hashlib.sha256(f.read_bytes()).hexdigest()} for f in sorted((work/'private').rglob('*')) if f.is_file() and f.suffix not in ('.pdb','.dbg') and '.dSYM' not in str(f)]
report={'rid':rid,'dotnet_sdk':subprocess.check_output(['dotnet','--version'],text=True).strip(),'host':platform.platform(),'scope':'Experimental catalog/archival manager and GUI+Qt smoke harness, not feature parity with Rust installer. One public executable with private extracted AOT app/native libraries/Qt. Unsigned artifacts, symbols excluded.','csharp':{'executable_bytes':exe.stat().st_size,'download_zip_bytes':(work/'csharp-application.zip').stat().st_size,'executable_sha256':hashlib.sha256(exe.read_bytes()).hexdigest(),'private_files':native,'payload_zip_bytes':(work/'application.zip').stat().st_size},'rust':{'executable_bytes':rust[0].stat().st_size,'download_zip_bytes':a.baseline.stat().st_size,'executable_sha256':hashlib.sha256(rust[0].read_bytes()).hexdigest(),'baseline_commit':'e40b48f7f369dceb6e46add87102590ca1f3eeb5','baseline_run':46082370013},'assets':{f.name:f.stat().st_size for f in assets.iterdir() if f.suffix=='.zip'}}
# Record all private Windows imports too; hosted SDK installs must not hide a VC runtime requirement.
if os.name=='nt':
 from audit_client_dependencies import pe_imports,WINDOWS_SYSTEM
 files=[exe,*[f for f in (work/'private').iterdir() if f.suffix.lower() in ('.exe','.dll')]]
 private={f.name.lower() for f in files}
 imports={f.name:pe_imports(f) for f in files}
 unresolved={name:[n for n in deps if n not in WINDOWS_SYSTEM and n not in private and not n.startswith(('api-ms-win-','ext-ms-win-'))] for name,deps in imports.items()}
 (work/'windows-imports.json').write_text(json.dumps({'imports':imports,'unresolved':unresolved},indent=2)+'\n')
 assert not any(unresolved.values()),unresolved
(work/'sizes.json').write_text(json.dumps(report,indent=2)+'\n')
(work/'app-path.txt').write_text(str(exe))
print(json.dumps(report,indent=2))
