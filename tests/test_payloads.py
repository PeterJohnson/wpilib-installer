import io
import base64
import hashlib
import subprocess
import sys
import json
import os
from pathlib import Path
import stat
import tarfile
import tempfile
import unittest
import zipfile

from installer_tooling.common import digest
from installer_tooling.dashboard_packages import binary_architectures, package_release
from installer_tooling.payload import inventory, verify_installed, validate_ownership
from installer_tooling.release_archive import extract
from installer_tooling.macos_metadata import records, restore_script, verify


class PayloadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def tar(self, entries):
        archive = self.root / 'app.tar'
        with tarfile.open(archive, 'w') as stream:
            for name, kind, value in entries:
                info = tarfile.TarInfo(name)
                info.mode = 0o755
                if kind == 'link':
                    info.type, info.linkname = tarfile.SYMTYPE, value
                    stream.addfile(info)
                elif kind == 'hardlink':
                    info.type, info.linkname = tarfile.LNKTYPE, value
                    stream.addfile(info)
                else:
                    info.size = len(value)
                    stream.addfile(info, io.BytesIO(value))
        return archive

    def test_archive_attacks_never_leave_an_output(self):
        cases = [
            [('../escape', 'file', b'x')],
            [('C:/escape', 'file', b'x')],
            [('file', 'file', b'x'), ('FILE', 'file', b'y')],
            [('app/link', 'link', '../../escape')],
            [('app/link', 'link', '/tmp/escape')],
            [('app/link', 'link', 'missing')],
            [('app/a', 'link', 'b'), ('app/b', 'link', 'a')],
            [('app/link', 'link', '.'), ('app/link/file', 'file', b'x')],
            [('app/file', 'file', b'x'), ('app/hard', 'hardlink', 'app/file')],
        ]
        for entries in cases:
            with self.subTest(entries=entries), self.assertRaises(ValueError):
                extract(self.tar(entries), self.root / 'out')
            self.assertFalse((self.root / 'out').exists())

    @unittest.skipIf(os.name == 'nt', 'macOS symlink payloads are packaged on POSIX hosts')
    def test_framework_symlinks_modes_and_signed_content_survive(self):
        archive = self.tar([
            ('App.framework/Versions/A/binary', 'file', b'executable'),
            ('App.framework/Versions/Current', 'link', 'A'),
            ('App.framework/binary', 'link', 'Versions/Current/binary')])
        extract(archive, self.root / 'out')
        records = inventory(self.root / 'out')
        package = {'owned_files': list(records), 'file_records': records}
        self.assertEqual(verify_installed(self.root / 'out', package), records)
        self.assertTrue(records['App.framework/Versions/A/binary']['executable'])
        self.assertEqual(records['App.framework/binary'], {'type': 'symlink', 'target': 'Versions/Current/binary'})
        binary = self.root / 'out/App.framework/Versions/A/binary'
        binary.chmod(0o644)
        with self.assertRaisesRegex(ValueError, 'differs from signed'):
            verify_installed(self.root / 'out', package)
        binary.chmod(0o755)
        binary.write_bytes(b'tampered')
        with self.assertRaisesRegex(ValueError, 'differs from signed'):
            verify_installed(self.root / 'out', package)

    def test_pax_signature_metadata_is_preserved_as_data_without_appledouble_files(self):
        archive = self.root / 'signed.tar'
        signature = b'original upstream signature'
        with tarfile.open(archive, 'w', format=tarfile.PAX_FORMAT) as stream:
            info = tarfile.TarInfo('App/resource')
            info.size = 3
            info.pax_headers = {'LIBARCHIVE.xattr.com.apple.cs.CodeSignature': base64.b64encode(signature).decode().rstrip('=')}
            stream.addfile(info, io.BytesIO(b'app'))
            sidecar = tarfile.TarInfo('App/._resource')
            sidecar.size = 4
            stream.addfile(sidecar, io.BytesIO(b'\x00\x05\x16\x07'))
        attributes = extract(archive, self.root / 'out')
        self.assertFalse((self.root / 'out/App/._resource').exists())
        self.assertEqual((self.root / 'out/App/resource').read_bytes(), b'app')
        expected = {'App/resource': {'com.apple.cs.CodeSignature': signature.hex()}}
        self.assertEqual(attributes, expected)
        self.assertEqual(records(attributes)['App/resource']['com.apple.cs.CodeSignature'],
                         {'size': len(signature), 'sha256': hashlib.sha256(signature).hexdigest()})
        script = restore_script(attributes)
        self.assertIn('/usr/bin/xattr -wx com.apple.cs.CodeSignature', script)
        with self.assertRaisesRegex(ValueError, 'Unsupported'):
            restore_script({'App/resource': {'com.apple.quarantine': '01'}})

    @unittest.skipUnless(sys.platform == 'darwin', 'Native macOS xattr round trip')
    def test_native_signature_xattrs_round_trip_and_tampering(self):
        root = self.root / "root with spaces ' and $chars"
        root.mkdir()
        file = root / "resource with ' quote"
        file.write_bytes(b'resource')
        attributes = {file.name: {'com.apple.cs.test': b'original signature'.hex()}}
        script = self.root / 'restore.sh'
        script.write_text(restore_script(attributes))
        subprocess.run(['/bin/sh', script, root], check=True)
        expected = records(attributes)[file.name]
        verify(file, expected)
        subprocess.run(['/usr/bin/xattr', '-wx', 'com.apple.cs.test', b'tampered signature'.hex(), file], check=True)
        with self.assertRaisesRegex(ValueError, 'signature attribute'):
            verify(file, expected)

    def test_ownership_rejects_cross_package_link_or_file_ancestors(self):
        with self.assertRaisesRegex(ValueError, 'nested beneath'):
            validate_ownership(['tools/Link', 'TOOLS/link/data'])
        with self.assertRaisesRegex(ValueError, 'Conflicting'):
            validate_ownership(['tools/file', 'TOOLS/FILE'])

    def test_zip_executable_permissions_and_no_following_archive_links(self):
        archive = self.root / 'app.zip'
        with zipfile.ZipFile(archive, 'w') as stream:
            info = zipfile.ZipInfo('app/tool')
            info.external_attr = (stat.S_IFREG | 0o755) << 16
            stream.writestr(info, b'executable')
        extract(archive, self.root / 'out')
        self.assertEqual((self.root / 'out/app/tool').read_bytes(), b'executable')
        if os.name != 'nt':
            self.assertTrue((self.root / 'out/app/tool').stat().st_mode & 0o111)
        with zipfile.ZipFile(archive, 'w') as stream:
            info = zipfile.ZipInfo('link')
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            stream.writestr(info, '../escape')
        with self.assertRaises(ValueError):
            extract(archive, self.root / 'bad')
        self.assertFalse((self.root / 'bad').exists())

    def test_recipe_binds_asset_license_version_architecture_and_provenance(self):
        executable = bytearray(64)
        executable[:6] = b'\x7fELF\x02\x01'
        executable[18:20] = (62).to_bytes(2, 'little')
        archive = self.tar([('elastic_dashboard', 'file', bytes(executable))])
        license_path = self.root / 'LICENSE'
        license_path.write_text('upstream license')
        def record(path):
            return {'url': path.as_uri(), 'sha256': digest(path), 'size': path.stat().st_size}
        lock = {'schema': 1, 'publishers': {'elastic': {'repository': 'Gold872/elastic_dashboard',
            'component': 'org.wpilib.elastic', 'releases': [{'tag': 'v1.2.3', 'version': '1.2.3',
            'assets': {'linux-x64': record(archive)}, 'license': record(license_path)}]}}}
        path = self.root / 'lock.json'
        path.write_text(json.dumps(lock))
        spec = package_release(path, 'elastic', 'linux-x64', 'v1.2.3', self.root / 'package', self.root / 'cache')
        package = json.loads(spec.read_text())['packages'][0]
        self.assertEqual(package['version'], '1.2.3')
        self.assertEqual(package['upstream']['asset'], record(archive))
        self.assertEqual((spec.parent / 'payload/licenses/elastic/LICENSE').read_text(), 'upstream license')
        self.assertEqual(binary_architectures(spec.parent / 'payload/tools/elastic/elastic_dashboard'), ('linux', {'x64'}))
        archive.write_bytes(b'corrupt')
        # Force a new cache to prove source digest enforcement.
        with self.assertRaises(ValueError):
            package_release(path, 'elastic', 'linux-x64', 'v1.2.3', self.root / 'bad', self.root / 'empty-cache')
        with self.assertRaisesRegex(ValueError, 'not pinned'):
            package_release(path, 'elastic', 'windows-x64', 'v1.2.3', self.root / 'missing', self.root / 'cache')


if __name__ == '__main__':
    unittest.main()
