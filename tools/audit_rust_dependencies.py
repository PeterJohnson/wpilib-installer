"""Fail builds whose manager/private worker needs a non-bundled platform runtime."""
import argparse
import json
from pathlib import Path
import re
import struct
import subprocess
import sys

# ComBase is an OS component since Windows 8 (Microsoft RoRegisterActivationFactories requirements).
WINDOWS_SYSTEM = set(('kernel32 user32 gdi32 gdi32full shell32 advapi32 ole32 oleaut32 combase ws2_32 '
    'iphlpapi userenv ntdll bcrypt bcryptprimitives crypt32 cryptbase cryptnet wintrust dwmapi '
    'shlwapi shcore comdlg32 comctl32 uxtheme winmm version netapi32 secur32 imm32 setupapi '
    'cfgmgr32 powrprof psapi dnsapi msvcrt ucrtbase rpcrt4 ntasn1 ncrypt d3d11 d3d12 dxgi '
    'd3dcompiler_47 dxcore opengl32 glu32 wsock32 cabinet normaliz ntmarta imagehlp dbghelp '
    'dbgcore winhttp wininet urlmon wtsapi32 hid usp10 msimg32 dwrite d2d1 propsys avrt authz d3d9 mpr uiautomationcore').split())
WINDOWS_SYSTEM = {n + '.dll' for n in WINDOWS_SYSTEM} | {'winspool.drv'}
LINUX_BASE = {'libc.so.6', 'libm.so.6', 'libpthread.so.0', 'libdl.so.2', 'librt.so.1',
              'libutil.so.1', 'libresolv.so.2', 'ld-linux-x86-64.so.2', 'libgcc_s.so.1'}


def pe_imports(path):
    data = Path(path).read_bytes()
    def u32(at): return struct.unpack_from('<I', data, at)[0]
    def u16(at): return struct.unpack_from('<H', data, at)[0]
    pe = u32(0x3c)
    if data[pe:pe+4] != b'PE\0\0': raise ValueError('Not a PE executable')
    optional, count = pe + 24, u16(pe + 6)
    pe64 = u16(optional) == 0x20b
    directories = optional + (112 if pe64 else 96)
    image_base = struct.unpack_from('<Q' if pe64 else '<I', data, optional + (24 if pe64 else 28))[0]
    sections = optional + u16(pe + 20)
    def offset(rva):
        for i in range(count):
            at = sections + 40*i
            virtual, address, raw_size, raw = struct.unpack_from('<IIII', data, at + 8)
            if address <= rva < address + max(virtual, raw_size): return raw + rva - address
        raise ValueError(f'Unmapped PE RVA {rva}')
    names = set()
    for index, stride, name_at in [(1,20,12),(13,32,4)]:
        rva = u32(directories + index*8)
        if not rva: continue
        at = offset(rva)
        while any(data[at:at+stride]):
            name_rva = u32(at+name_at)
            if index == 13 and not u32(at) & 1: name_rva -= image_base
            start = offset(name_rva)
            end = data.index(b'\0', start, start+256)
            names.add(data[start:end].decode('ascii').lower())
            at += stride
    return sorted(names)


def audit(path, worker=False, runtime=None):
    path = Path(path)
    if path.suffix == '.app':
        binaries = list((path / 'Contents/MacOS').iterdir())
        if len(binaries) != 1: raise ValueError('Ambiguous app executable')
        path = binaries[0]
    if path.read_bytes()[:2] == b'MZ':
        imports = pe_imports(path)
        invalid = [n for n in imports if n not in WINDOWS_SYSTEM and not n.startswith(('api-ms-win-', 'ext-ms-win-'))]
        if invalid: raise ValueError(f'Non-system Windows runtime imports in {path}: {invalid}')
        return {'kind':'PE','imports':imports,'policy':'Windows 10/11 inbox DLLs only; no VC redistributable'}
    if sys.platform == 'darwin':
        libraries = subprocess.check_output(['otool','-L',str(path)],text=True)
        imports = re.findall(r'^\s+(\S+) \(compatibility version',libraries,re.M)
        if not imports or any(not n.startswith(('/usr/lib/','/System/Library/')) for n in imports):
            raise ValueError(f'Non-system macOS library: {libraries}')
        commands = subprocess.check_output(['otool','-l',str(path)],text=True)
        versions = re.findall(r'\bminos (\d+\.\d+(?:\.\d+)?)',commands)
        versions += re.findall(r'cmd LC_VERSION_MIN_MACOSX\s+cmdsize \d+\s+version (\S+)',commands)
        if not versions or any(tuple(map(int,v.split('.'))) > (13,0,0) for v in versions):
            raise ValueError(f'Binary exceeds the macOS 13 deployment baseline: {versions}')
        return {'kind':'Mach-O','imports':imports,'minimum_os':versions,'policy':'macOS inbox frameworks/libraries only'}
    dynamic = subprocess.check_output(['readelf','-d',str(path)],text=True)
    imports = re.findall(r'\(NEEDED\).*\[(.+?)\]',dynamic)
    bundled = set()
    if runtime:
        manifest = json.loads(Path(runtime).read_text())
        bundled = {Path(n).name for n in manifest['files'] if n.startswith('lib/')}
    invalid = set(imports) - LINUX_BASE - (bundled if worker else set())
    if invalid: raise ValueError(f'Unbundled ELF dependency in {path}: {invalid}')
    symbols = subprocess.check_output(['readelf','--version-info',str(path)],text=True)
    glibc = sorted({tuple(map(int,v.split('.'))) for v in re.findall(r'\bGLIBC_(\d+\.\d+(?:\.\d+)?)',symbols)})
    if glibc and glibc[-1] > (2,35): raise ValueError(f'Binary exceeds glibc 2.35: {glibc[-1]}')
    return {'kind':'ELF','imports':imports,'maximum_glibc':glibc[-1] if glibc else None,
            'policy':'base GNU/Linux ABI; worker dependencies embedded'}


def check(manager, worker, report, runtime=None):
    value={'schema':1,'manager':audit(manager,runtime=runtime),
           'private_qt_worker':audit(worker,worker=True,runtime=runtime)}
    Path(report).write_text(json.dumps(value,indent=2)+'\n')
    print(json.dumps(value,indent=2),flush=True)
    return value


if __name__ == '__main__':
    p=argparse.ArgumentParser(description=__doc__)
    for name in ['manager','worker','report']:p.add_argument('--'+name,type=Path,required=True)
    p.add_argument('--runtime',type=Path)
    a=p.parse_args()
    check(a.manager,a.worker,a.report,a.runtime)
