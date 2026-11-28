"""Build the selected NativeAOT C#/Avalonia application with a private Qt engine.
Python, .NET SDK, Docker (Linux) and IFW are build dependencies only.
"""
import argparse, hashlib, json, os, platform, shutil, subprocess, sys, zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

def zip_tree(source, destination):
    with zipfile.ZipFile(destination, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for f in sorted(source.rglob('*')):
            if f.is_file() and f.suffix not in ('.pdb', '.dbg') and '.dSYM' not in str(f):
                z.write(f, f.relative_to(source))

def publish(assets, work):
    rid = 'win-x64' if os.name == 'nt' else ('osx-arm64' if platform.machine() == 'arm64' else 'osx-x64') if sys.platform == 'darwin' else 'linux-x64'
    env = {**os.environ, 'MACOSX_DEPLOYMENT_TARGET':'13.0', 'DOTNET_CLI_TELEMETRY_OPTOUT':'1'}
    def project(name, output, prop):
        subprocess.run(['dotnet', 'publish', str(REPO / 'client-dotnet' / name), '-c', 'Release', '-r', rid, '-o', str(output), prop], cwd=REPO / 'client-dotnet', env=env, check=True)
    project('app/App.csproj', work / 'private', f'-p:ManagerAssets={assets}')
    if sys.platform == 'linux':
        for name in ('libssl.so.3','libcrypto.so.3'):
            shutil.copy2(Path('/usr/lib/x86_64-linux-gnu') / name, work / 'private' / name)
        shutil.copy2('/usr/share/doc/libssl3/copyright',work / 'private/OpenSSL-NOTICE.txt')
        compat=work / 'private/x11-compat';compat.mkdir(exist_ok=True)
        with zipfile.ZipFile(assets / 'runtime.zip') as z:
            for name in ('libxcb-xkb.so.1','libxkbcommon.so.0','libxkbcommon-x11.so.0'):
                (compat / name).write_bytes(z.read('lib/'+name))

    zip_tree(work / 'private', work / 'application.zip')
    project('launcher/Launcher.csproj', work / 'launcher', f'-p:Payload={work / "application.zip"}')

def build(source, ifw, output, assets):
    from installer_tooling.qt_engine import build_installer
    from installer_tooling.offline_acceptance import host_target
    from installer_tooling.registry_updates import validate_source

    source, output, assets = (Path(p).resolve() for p in (source, output, assets))
    pin=json.loads(source.read_text());validate_source(pin)
    if pin['target'] != host_target(): raise ValueError('Bootstrap must match build host')
    assets.mkdir(parents=True, exist_ok=True);shutil.copy2(source, assets / 'bootstrap.json')
    suffix='.exe' if os.name=='nt' else '.app' if sys.platform=='darwin' else ''
    engine=build_installer(ifw,assets / ('engine'+suffix),maintenance_name='.wpilib-engine')
    with zipfile.ZipFile(assets / 'engine.zip','w',zipfile.ZIP_DEFLATED,compresslevel=1) as z:
        for f in sorted(engine.rglob('*')) if engine.is_dir() else [engine]:
            if f.is_file():z.write(f,f.relative_to(assets))
    work=assets.parent / 'aot';work.mkdir(parents=True,exist_ok=True)
    if sys.platform=='linux':
        (work / 'runtime-dir').mkdir(mode=0o700,exist_ok=True)
        # Runtime notices/dependencies come from the same minimal image as the Qt worker.
        runtime='wpilib-installer-linux-runtime:22.04-rust-1.100.0'
        subprocess.run(['docker','build','-f',str(REPO / '.github/containers/linux-runtime.Dockerfile'),'-t',runtime,str(REPO)],check=True)
        subprocess.run(['docker','run','--rm','--user',f'{os.getuid()}:{os.getgid()}', '-v',f'{REPO}:/work:ro','-v',f'{assets}:/assets',runtime,'python3','tools/package_linux_runtime.py','--engine','/assets/engine','--output','/assets/runtime.zip'],check=True)
        image='wpilib-installer-dotnet:22.04-10.0.404'
        subprocess.run(['docker','build','-f',str(REPO / '.github/containers/dotnet-client.Dockerfile'),'-t',image,str(REPO)],check=True)
        subprocess.run(['docker','run','--rm','--user',f'{os.getuid()}:{os.getgid()}', '-v',f'{REPO}:/work','-v',f'{assets}:/assets','-v',f'{work}:/aot', '-v','/etc/passwd:/etc/passwd:ro','-v','/etc/group:/etc/group:ro','-e','DOTNET_CLI_HOME=/aot/home','-e','NUGET_PACKAGES=/aot/nuget','-e','XDG_RUNTIME_DIR=/aot/runtime-dir',image,'python3','-c','import sys; sys.path.insert(0,"tools"); from build_client import publish; from pathlib import Path; publish(Path("/assets"),Path("/aot"))'],check=True)
    else: publish(assets,work)
    if sys.platform=='darwin':
        app=output / 'WPILib Manager.app';exe=app / 'Contents/MacOS/wpilib-manager';exe.parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(REPO / 'client/Info.plist',app / 'Contents/Info.plist')
    else:
        exe=output / ('WPILibManager.exe' if os.name=='nt' else 'wpilib-manager');exe.parent.mkdir(parents=True,exist_ok=True)
    shutil.copy2(work / 'launcher' / ('WPILibManager.exe' if os.name=='nt' else 'WPILibManager'),exe)
    from audit_client_dependencies import check, pe_imports, WINDOWS_SYSTEM
    check(exe,engine,assets / 'dependency-audit.json',assets / 'runtime.json' if sys.platform=='linux' else None)
    if os.name=='nt':
        files=[exe,*[f for f in (work / 'private').iterdir() if f.suffix.lower() in ('.exe','.dll')]]
        private={f.name.lower() for f in files};imports={f.name:pe_imports(f) for f in files}
        unresolved={name:[n for n in deps if n not in WINDOWS_SYSTEM and n not in private and not n.startswith(('api-ms-win-','ext-ms-win-'))] for name,deps in imports.items()}
        (assets / 'windows-imports.json').write_text(json.dumps({'imports':imports,'unresolved':unresolved},indent=2)+'\n')
        if any(unresolved.values()):raise ValueError(unresolved)
    zip_tree(output,output.parent / 'manager-application.zip')
    (assets / 'sizes.json').write_text(json.dumps({'application_bytes':exe.stat().st_size,'download_zip_bytes':(output.parent / 'manager-application.zip').stat().st_size,'sha256':hashlib.sha256(exe.read_bytes()).hexdigest(),'private_files':{f.name:f.stat().st_size for f in (work / 'private').iterdir() if f.is_file() and f.suffix not in ('.pdb','.dbg')}},indent=2)+'\n')
    print(exe,flush=True);return exe

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source',type=Path,required=True);p.add_argument('--ifw',type=Path,required=True)
    p.add_argument('--output',type=Path,default=Path('build/client/download'));p.add_argument('--assets',type=Path,default=Path('build/client/assets'))
    a=p.parse_args();build(a.source,a.ifw,a.output,a.assets)
