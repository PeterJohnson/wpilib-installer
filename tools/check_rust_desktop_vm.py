"""Install stock Ubuntu Desktop from a pinned ISO and run the manager offline.
No packages are added to the guest. Test tooling is its preinstalled Python stdlib.
QEMU/KVM and xorriso are host-only dependencies; use --tcg for local debugging.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import time

IMAGES = {
    '24.04.4': ('24.04', '3a4c9877b483ab46d7c3fbe165a0db275e1ae3cfe56a5657e5a47c2f99a99d1e'),
    '26.04.1': ('26.04', '601e30fbf5d97759367c632e2c33630665039b7e2158fd068403da3ccf1bda1f'),
}


def digest(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for b in iter(lambda:f.read(4*1024*1024),b''): h.update(b)
    return h.hexdigest()


def run(*args, **kw):
    return subprocess.run(list(map(str,args)),check=True,**kw)


def check(a):
    work=a.work.resolve();work.mkdir(parents=True)
    share=work/'share';share.mkdir();share.chmod(0o777)
    for name,p in [('wpilib-manager',a.app),('baseline.zip',a.baseline),('candidate.zip',a.candidate),
                   ('check_rust_desktop_guest.py',Path(__file__).with_name('check_rust_desktop_guest.py'))]:
        shutil.copy2(p,share/name)
    (share/'wpilib-manager').chmod(0o755)
    series,expected=IMAGES[a.release]
    a.iso_dir.mkdir(parents=True,exist_ok=True)
    iso=a.iso_dir.resolve()/f'ubuntu-{a.release}-desktop-amd64.iso'
    url=f'https://releases.ubuntu.com/{series}/{iso.name}'
    if not iso.exists():
        partial=iso.with_suffix('.iso.part')
        run('curl','-fL','--retry','3',url,'-o',partial)
        partial.rename(iso)
    actual=digest(iso)
    if actual != expected: raise ValueError(f'ISO checksum mismatch: {actual}')
    evidence={'schema':1,'iso':{'url':url,'sha256':actual},'application_sha256':digest(a.app),
              'hypervisor':'qemu-tcg' if a.tcg else 'qemu-kvm', 'installation_source':'ubuntu-desktop',
              'guest_added_packages':[], 'network':'no virtual network interface during installation or acceptance'}
    (work/'vm.json').write_text(json.dumps(evidence,indent=2)+'\n')
    for name in ['vmlinuz','initrd','install-sources.yaml']:
        run('xorriso','-osirrox','on','-indev',iso,'-extract','/casper/'+name,work/name,
            stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    # Only autologin and the test entry point differ from the installed desktop.
    setup='''#!/bin/bash
set -eux
mkdir -p /target/mnt/wpilib-test /target/home/tester/.config/autostart
printf '%s\\n' 'wpilibshare /mnt/wpilib-test 9p trans=virtio,version=9p2000.L,msize=262144 0 0' >> /target/etc/fstab
cat > /target/etc/gdm3/custom.conf <<'EOF'
[daemon]
AutomaticLoginEnable=true
AutomaticLogin=tester
EOF
cat > /target/home/tester/.config/autostart/wpilib-acceptance.desktop <<'EOF'
[Desktop Entry]
Type=Application
Name=WPILib acceptance
Exec=/usr/bin/python3 /mnt/wpilib-test/check_rust_desktop_guest.py
X-GNOME-Autostart-enabled=true
EOF
touch /target/home/tester/.config/gnome-initial-setup-done
chown -R 1000:1000 /target/home/tester
cp -r /var/log/installer /mnt/wpilib-test/install-logs || true
touch /mnt/wpilib-test/installed
'''
    (share/'prepare.sh').write_text(setup)
    seed=work/'seed';seed.mkdir()
    # A public throwaway test credential, never used outside the networkless VM.
    password=subprocess.check_output(['openssl','passwd','-6','-salt','wpilibtest','wpilib-test-only'],text=True).strip()
    config={'autoinstall':{'version':1,'refresh-installer':{'update':False},
            'source':{'id':'ubuntu-desktop','search_drivers':False},
            'locale':'en_US.UTF-8','keyboard':{'layout':'us'},'timezone':'Etc/UTC',
            'identity':{'hostname':'wpilib-desktop','username':'tester','realname':'Installer acceptance','password':password},
            'storage':{'layout':{'name':'direct'}},'ssh':{'install-server':False},
            'apt':{'geoip':False,'fallback':'offline-install'},'shutdown':'poweroff',
            'late-commands':[
                'mkdir -p /mnt/wpilib-test',
                'mount -t 9p -o trans=virtio,version=9p2000.L wpilibshare /mnt/wpilib-test',
                'bash /mnt/wpilib-test/prepare.sh'],
            'error-commands':['mkdir -p /mnt/wpilib-test; mount -t 9p -o trans=virtio wpilibshare /mnt/wpilib-test || true',
                              'cp -r /var/log/installer /mnt/wpilib-test/install-error-logs || true']}}
    (seed/'user-data').write_text('#cloud-config\n'+json.dumps(config,indent=2)+'\n')
    (seed/'meta-data').write_text('instance-id: wpilib-desktop-acceptance\nlocal-hostname: wpilib-desktop\n')
    run('xorriso','-as','mkisofs','-quiet','-V','cidata','-J','-r','-o',work/'seed.iso',seed)
    disk=work/'desktop.qcow2'
    run('qemu-img','create','-f','qcow2',disk,'32G')
    if not a.tcg and not os.access('/dev/kvm',os.R_OK|os.W_OK):
        raise RuntimeError('KVM is required for CI; --tcg permits slower local debugging')
    common=['qemu-system-x86_64','-machine','q35','-accel','tcg,thread=multi' if a.tcg else 'kvm',
            '-cpu','max' if a.tcg else 'host','-smp','4','-m','6144',
            '-drive',f'file={disk},format=qcow2,if=virtio', '-device','virtio-vga',
            '-display','none','-monitor','none','-nic','none','-no-reboot',
            '-fsdev',f'local,id=share,path={share},security_model=mapped-xattr',
            '-device','virtio-9p-pci,fsdev=share,mount_tag=wpilibshare']

    def vm(phase, extra, complete=None):
        started=time.monotonic()
        with (work/(phase+'-qemu.log')).open('w') as log:
            p=subprocess.Popen([*common,'-serial',f'file:{work/(phase+"-serial.log")}',*extra],stdout=log,stderr=subprocess.STDOUT)
            try:
                while p.poll() is None:
                    if complete and complete.exists(): break
                    if time.monotonic()-started > a.timeout: raise TimeoutError(f'{phase} timed out; inspect serial/install logs')
                    time.sleep(2)
                if complete:
                    assert complete.exists(),f'{phase}: QEMU exited without a guest report'
                else: assert p.returncode == 0,f'{phase}: QEMU exited {p.returncode}'
            finally:
                if p.poll() is None:
                    p.terminate()
                    try:p.wait(timeout=20)
                    except subprocess.TimeoutExpired:p.kill();p.wait()
        evidence[phase+'_seconds']=round(time.monotonic()-started,1)
        (work/'vm.json').write_text(json.dumps(evidence,indent=2)+'\n')

    vm('install',['-drive',f'file={iso},media=cdrom,readonly=on',
                  '-drive',f'file={work/"seed.iso"},media=cdrom,readonly=on',
                  '-kernel',str(work/'vmlinuz'),'-initrd',str(work/'initrd'),
                  '-append','boot=casper autoinstall ds=nocloud console=ttyS0,115200n8 ---'])
    assert (share/'installed').exists(),'Desktop installation did not complete'
    vm('desktop',[],share/'report.json')
    report=json.loads((share/'report.json').read_text())
    assert report['status']=='passed',report
    assert report['application_sha256']==evidence['application_sha256']
    assert f'VERSION_ID="{series}"' in report['os_release'],report['os_release']
    assert len(report['checks'])==18 and all(c['passed'] for c in report['checks']),report
    evidence['result']='passed';evidence['guest']=report
    (work/'report.json').write_text(json.dumps(evidence,indent=2)+'\n')
    print(f'PASS Ubuntu Desktop {a.release}: {len(report["checks"])} checks',flush=True)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    for name in ['app','baseline','candidate','work','iso-dir']:p.add_argument('--'+name,type=Path,required=True)
    p.add_argument('--release',choices=IMAGES,required=True)
    p.add_argument('--tcg',action='store_true')
    p.add_argument('--timeout',type=int,default=2400)
    check(p.parse_args())
