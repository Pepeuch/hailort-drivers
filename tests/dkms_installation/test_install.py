"""D-03: real filesystem/rename/flock; fixture-only DKMS and privilege check."""
import argparse
import ctypes
import ast
import hashlib
import json
from contextlib import contextmanager, redirect_stderr, redirect_stdout
import importlib.util
import io
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('installer', ROOT / 'linux/pcie/dkms_install.py')
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


original_directory = installer.directory


@contextmanager
def fixture_directory(path, trusted=False):
    # Production ancestors are root-owned /usr, /var and /run. The workbench
    # ancestors are shared developer workspace directories; validate only the
    # fixture subtree while retaining the real no-follow traversal.
    path = Path(os.path.abspath(path))
    fixture = installer.SOURCE_ROOT.parent
    if trusted:
        for ancestor in (path, *path.parents):
            if ancestor.is_relative_to(fixture):
                info = ancestor.lstat()
                if info.st_uid != os.geteuid() or info.st_mode & 0o022:
                    raise installer.InstallError('unsafe destination ancestor: ' + str(ancestor))
    with original_directory(path) as fd:
        yield fd


def configure(root):
    installer.SOURCE_ROOT = root / 'sources'
    installer.DKMS_ROOT = root / 'dkms'
    installer.LOCK_ROOT = root / 'run/installer'


def arguments(root, version='4.24.0', kernel='kernel-a'):
    return argparse.Namespace(version=version, kernel=kernel, headers=str(root / 'headers' / kernel),
                              **{name: str(root / 'input' / name) for name in ('pcie', 'common', 'vdma', 'utils', 'includes')})


def worker(root):
    configure(root)
    descendants = bool(os.environ.get('INSTALL_DESCENDANT'))
    if descendants:
        # Test-only subreaper: safely own/reap orphaned fixture descendants.
        if ctypes.CDLL(None).prctl(36, 1, 0, 0, 0) != 0:
            raise RuntimeError('test subreaper setup failed')
    original = installer.copy_tree
    def hold(fd, target):
        (root / 'ready').touch()
        while not (root / 'release').exists():
            time.sleep(0.01)
        original(fd, target)
    for sig in installer.SIGNALS:
        signal.signal(sig, installer.interrupted)
    with patch.object(installer, 'require_root'), patch.object(installer, 'directory', side_effect=fixture_directory), patch.object(installer, 'copy_tree', side_effect=hold):
        try:
            installer.install(arguments(root))
        finally:
            if descendants and (root / 'descendant-ready').exists():
                pid = int((root / 'descendant-ready').read_text())
                deadline = time.monotonic() + 1
                status = None
                while time.monotonic() < deadline:
                    status = os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
                    if status is not None:
                        break
                    time.sleep(0.01)
                (root / 'descendant-result').write_text(json.dumps({'survived': status is None}))
                # The fixture descendant is now our unreaped child. Its PID is
                # pinned until waitpid; no signal is sent after reaping.
                if status is None:
                    os.kill(pid, signal.SIGKILL)
                deadline = time.monotonic() + 3
                while os.waitpid(pid, os.WNOHANG)[0] == 0:
                    if time.monotonic() > deadline:
                        raise RuntimeError('fixture descendant cleanup timeout')
                    time.sleep(0.01)


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.assertNotEqual(0, os.geteuid())
        temporary = ROOT / '.workbench/tmp'
        temporary.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(prefix='d03 fixture ', dir=temporary)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        configure(self.root)
        for name in ('sources', 'dkms', 'run', 'bin'):
            (self.root / name).mkdir(mode=0o755)
        self.args = arguments(self.root)
        for kernel in ('kernel-a', 'kernel-b'):
            base = self.root / 'headers' / kernel
            for name in ('Makefile', 'Module.symvers', 'include/config/auto.conf',
                         'include/generated/autoconf.h', 'include/generated/utsrelease.h'):
                path = base / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(f'#define UTS_RELEASE "{kernel}"' if name.endswith('utsrelease.h') else 'fixture')
        for kernel in ('kernel-a', 'kernel-b'):
            for name in ('scripts/basic/fixdep', 'scripts/mod/modpost'):
                path = self.root / 'headers' / kernel / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('fixture executable')
                path.chmod(0o700)
        files = {'pcie': ('Makefile', 'Kbuild', 'dkms.conf.in', 'src/pcie.c', 'src/fops.c', 'src/sysfs.c', 'src/nnc.c', 'src/soc.c'),
                 'common': ('hailo_ioctl_common.h', 'fw_validation.c', 'fw_operation.c', 'pcie_common.c', 'vdma_common.c', 'hailo_resource.c'),
                 'vdma': ('vdma.c', 'memory.c', 'ioctl.c'), 'utils': ('logs.c',), 'includes': ('public.h',)}
        for directory, names in files.items():
            for name in names:
                path = self.root / 'input' / directory / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text((ROOT / 'linux/pcie/dkms.conf.in').read_text() if name == 'dkms.conf.in' else name)
        stub = self.root / 'bin/dkms'
        stub.write_text('#!/bin/sh\nexec ' + shlex.quote(sys.executable) + ' -B ' + shlex.quote(str(ROOT / 'tests/dkms_installation/fake_dkms.py')) + ' "$@"\n')
        stub.chmod(0o700)
        self.environment = patch.dict(os.environ, INSTALL_FIXTURE=str(self.root), INSTALL_FAIL='', INSTALL_LOG='normal', INSTALL_HOLD='', INSTALL_DESCENDANT='',
                                      PATH=str(self.root / 'bin') + os.pathsep + os.environ['PATH'])
        self.environment.start()
        self.addCleanup(self.environment.stop)
        directories = patch.object(installer, 'directory', side_effect=fixture_directory)
        directories.start()
        self.addCleanup(directories.stop)
        self.privilege = patch.object(installer, 'require_root')
        self.privilege.start()
        self.addCleanup(self.privilege.stop)
        # Pre-existing versions, modules, dependency indexes, originals/backups.
        self.sentinels = []
        for version in ('4.22.0', '4.23.0'):
            for kernel in ('kernel-a', 'kernel-b'):
                for suffix in ('.ko', '.ko.gz', '.ko.xz', '.ko.zst'):
                    for directory in ('modules', 'original_module', 'restored'):
                        self.sentinels.append(self.root / directory / version / kernel / ('hailo_pci' + suffix))
                self.sentinels += [self.root / 'dkms/hailo_pci' / version / kernel / 'status',
                                   self.root / 'modules' / version / kernel / 'modules.dep']
            self.sentinels.append(self.root / 'sources' / ('hailo_pci-' + version) / 'keep.c')
        for path in self.sentinels:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(str(path.relative_to(self.root)).encode())
        self.before = {path: path.read_bytes() for path in self.sentinels}

    def destination(self, version='4.24.0'):
        return self.root / 'sources' / ('hailo_pci-' + version)

    def calls(self):
        path = self.root / 'calls'
        return path.read_text().splitlines() if path.exists() else []

    def invoke(self, args=None):
        output = io.StringIO()
        code = 0
        with redirect_stdout(output), redirect_stderr(output):
            try:
                installer.install(args or self.args)
            except (installer.InstallError, OSError) as error:
                code = error.code if isinstance(error, installer.InstallError) else 1
                output.write(str(error))
        for path, data in self.before.items():
            self.assertEqual(data, path.read_bytes(), str(path))
        if code != 75:
            self.assertEqual([], list((self.root / 'sources').glob('*.stage-*')))
        return code, output.getvalue()

    def rejected(self, text=None):
        code, output = self.invoke()
        self.assertNotEqual(0, code, output)
        if text:
            self.assertIn(text, output)
        self.assertFalse(self.destination().exists())
        self.assertEqual([], self.calls())

    def test_success_and_selected_kernel(self):
        self.assertEqual(0, self.invoke()[0])
        calls = self.calls()
        self.assertEqual(3, len(calls))
        self.assertTrue(calls[0].startswith('add -m hailo_pci -v 4.24.0'))
        for call in calls[1:]:
            self.assertIn('-k kernel-a --kernelsourcedir ' + self.args.headers, call)
        self.assertIn('dkms install: complete', (self.destination() / '.hailo-install.log').read_text())
        self.assertEqual((self.root / 'input/pcie/src/pcie.c').read_bytes(),
                         (self.destination() / 'hailort/drivers/linux/pcie/src/pcie.c').read_bytes())

    def test_invalid_versions(self):
        for value in ('', '../4.24.0', '4.24', '4.24.0\nother', '4.24.0;touch marker'):
            with self.subTest(value=value):
                self.args.version = value
                self.rejected('invalid driver version')

    def test_invalid_kernels(self):
        for value in ('', '../kernel', '-kernel', 'kernel\nother', "kernel';touch marker"):
            with self.subTest(value=value):
                self.args.kernel = value
                self.rejected('invalid kernel release')

    def test_root_required(self):
        with patch.object(installer, 'require_root', side_effect=installer.InstallError('should run as root')):
            self.rejected('should run as root')

    def test_real_root_guard(self):
        self.privilege.stop()
        self.rejected('should run as root')

    def test_missing_commands(self):
        original = installer.shutil.which
        for command in ('dkms', 'make', 'gcc', 'ld', 'as', 'ar', 'nm', 'objcopy', 'objdump', 'strip', 'depmod'):
            with self.subTest(command=command), patch.object(installer.shutil, 'which', side_effect=lambda x: None if x == command else original(x)):
                self.rejected('required tool missing')

    def test_missing_headers(self):
        for name in ('Makefile', 'Module.symvers', 'include/config/auto.conf', 'include/generated/autoconf.h', 'include/generated/utsrelease.h'):
            path = Path(self.args.headers) / name
            data = path.read_bytes()
            path.unlink()
            with self.subTest(name=name):
                self.rejected('kernel header')
            path.write_bytes(data)

    def test_mismatched_kernel_headers(self):
        self.args.headers = str(self.root / 'headers/kernel-b')
        self.rejected('do not match')

    def test_missing_source_directories(self):
        for name in ('pcie', 'common', 'vdma', 'utils', 'includes'):
            previous = getattr(self.args, name)
            setattr(self.args, name, str(self.root / 'absent'))
            with self.subTest(name=name):
                self.rejected('PR #56 is separate')
            setattr(self.args, name, previous)

    def test_missing_required_source_file(self):
        (self.root / 'input/pcie/src/pcie.c').unlink()
        self.rejected('missing required source file')

    def test_invalid_template(self):
        (self.root / 'input/pcie/dkms.conf.in').write_text('bad')
        self.rejected('invalid DKMS configuration')

    def test_source_symlinks_and_special_files(self):
        target = self.root / 'input/includes/unexpected'
        for kind in ('symlink', 'fifo', 'directory-link'):
            with self.subTest(kind=kind):
                if kind == 'fifo':
                    os.mkfifo(target)
                else:
                    target.symlink_to(self.root / ('input/common' if kind == 'directory-link' else 'absent'))
                self.rejected('unsupported source entry')
                target.unlink()

    def test_destination_collisions(self):
        target = self.destination()
        for kind in ('directory', 'file', 'symlink'):
            with self.subTest(kind=kind):
                if kind == 'directory':
                    target.mkdir()
                elif kind == 'file':
                    target.write_text('keep')
                else:
                    target.symlink_to(self.root / 'absent')
                code, output = self.invoke()
                self.assertNotEqual(0, code)
                self.assertIn('already exists', output)
                self.assertTrue(os.path.lexists(target))
                self.assertEqual([], self.calls())
                target.rmdir() if kind == 'directory' else target.unlink()

    def test_registration_collisions(self):
        target = self.root / 'dkms/hailo_pci/4.24.0'
        for kind in ('directory', 'file', 'symlink'):
            with self.subTest(kind=kind):
                target.mkdir() if kind == 'directory' else target.write_text('keep') if kind == 'file' else target.symlink_to(self.root / 'absent')
                self.rejected('already exists')
                self.assertTrue(os.path.lexists(target))
                target.rmdir() if kind == 'directory' else target.unlink()

    def test_destination_parent_symlink(self):
        parent = self.root / 'sources'
        parent.rename(self.root / 'saved-sources')
        parent.symlink_to(self.root / 'saved-sources')
        self.rejected()

    def test_unsafe_destination_permissions(self):
        (self.root / 'sources').chmod(0o777)
        self.rejected('unsafe destination ancestor')

    def test_no_available_space(self):
        with patch.object(installer.os, 'fstatvfs', return_value=argparse.Namespace(f_bavail=0, f_favail=1)):
            self.rejected('no available blocks')

    def test_copy_failure_at_each_source(self):
        original = installer.copy_tree
        for index, relative in enumerate(('pcie', 'common', 'vdma', 'utils', 'include')):
            def fail(fd, target):
                if target.name == relative:
                    target.mkdir()
                    (target / 'partial').write_text('partial')
                    raise OSError('injected copy failure ' + str(index))
                original(fd, target)
            with self.subTest(relative=relative), patch.object(installer, 'copy_tree', side_effect=fail):
                self.rejected('injected copy failure')
        self.assertEqual(0, self.invoke()[0])  # staging failures permit retry

    def test_source_change_during_copy(self):
        original = installer.copy_tree
        def change(fd, target):
            original(fd, target)
            if target.name == 'pcie':
                (target / 'Kbuild').write_text('corrupt copy')
        with patch.object(installer, 'copy_tree', side_effect=change):
            self.rejected('staged copy incomplete')

    def test_source_symlink_race(self):
        original = installer.copy_tree
        def change(fd, target):
            if target.name == 'pcie':
                path = self.root / 'input/pcie/Kbuild'
                path.unlink()
                path.symlink_to(self.root / 'input/common/hailo_ioctl_common.h')
            original(fd, target)
        with patch.object(installer, 'copy_tree', side_effect=change):
            self.rejected('unsupported source entry')

    def test_publication_error(self):
        with patch.object(installer, 'publish', side_effect=OSError('injected publication failure')):
            self.rejected('publication failure')

    def test_publication_race_never_replaces_destination(self):
        original = installer.publish
        for kind in ('directory', 'symlink'):
            def collide(stage, root, name):
                target = self.destination()
                target.mkdir() if kind == 'directory' else target.symlink_to(self.root / 'absent')
                original(stage, root, name)
            with self.subTest(kind=kind), patch.object(installer, 'publish', side_effect=collide):
                code, _ = self.invoke()
                self.assertNotEqual(0, code)
                self.assertTrue(os.path.lexists(self.destination()))
                self.assertEqual([], self.calls())
                self.destination().rmdir() if kind == 'directory' else self.destination().unlink()

    def test_replaced_destination_parent(self):
        original = installer.copy_tree
        moved = False
        def replace(fd, target):
            nonlocal moved
            original(fd, target)
            if not moved:
                moved = True
                (self.root / 'sources').rename(self.root / 'old-sources')
                (self.root / 'sources').mkdir()
        with patch.object(installer, 'copy_tree', side_effect=replace):
            # Sentinels remain in the pinned old parent, which is intentionally
            # retained. Verify directly instead of invoke's original path check.
            with self.assertRaisesRegex(installer.InstallError, 'parent replaced'):
                installer.install(self.args)
        self.assertEqual([], list((self.root / 'old-sources').glob('*.stage-*')))
        for path, data in self.before.items():
            target = self.root / 'old-sources' / path.relative_to(self.root / 'sources') if path.is_relative_to(self.root / 'sources') else path
            self.assertEqual(data, target.read_bytes())
        self.assertEqual([], self.calls())

    def test_dkms_add_build_install_failure_and_retry(self):
        for action in ('add', 'build', 'install'):
            with self.subTest(action=action), patch.dict(os.environ, INSTALL_FAIL=action):
                code, output = self.invoke()
                self.assertEqual(17, code)
                self.assertIn('No rollback attempted', output)
                self.assertTrue(self.destination().is_dir())
                log = self.destination() / '.hailo-install.log'
                self.assertIn('diagnostic from mocked ' + action, log.read_text())
                before = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
                self.assertNotEqual(0, self.invoke()[0])
                self.assertEqual(before, {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob('*') if p.is_file()})
            # Remove only test-owned results to test the next independent phase.
            import shutil
            shutil.rmtree(self.destination())
            shutil.rmtree(self.root / 'dkms/hailo_pci/4.24.0')
            (self.root / 'calls').unlink()

    def test_missing_and_unreadable_build_logs(self):
        for mode in ('missing', 'unreadable'):
            with self.subTest(mode=mode), patch.dict(os.environ, INSTALL_FAIL='build', INSTALL_LOG=mode):
                code, output = self.invoke()
                self.assertEqual(17, code)
                self.assertIn('if present/readable', output)
                self.assertIn('diagnostic from mocked build', (self.destination() / '.hailo-install.log').read_text())
                path = self.root / 'dkms/hailo_pci/4.24.0/build/make.log'
                if mode == 'unreadable':
                    self.assertEqual(0, path.stat().st_mode & 0o777)
                    path.chmod(0o600)
                    self.assertEqual('preserved build diagnostic', path.read_text())
                else:
                    self.assertFalse(path.exists())
            import shutil
            shutil.rmtree(self.destination())
            shutil.rmtree(self.root / 'dkms/hailo_pci/4.24.0')
            (self.root / 'calls').unlink()

    def test_two_new_versions_and_kernels(self):
        self.assertEqual(0, self.invoke()[0])
        self.assertEqual(0, self.invoke(arguments(self.root, '4.25.0', 'kernel-b'))[0])
        self.assertTrue(self.destination().exists())
        self.assertTrue(self.destination('4.25.0').exists())
        self.assertEqual(6, len(self.calls()))
        self.assertIn('-v 4.25.0 ', self.calls()[-1])
        self.assertIn(' -k kernel-b ', self.calls()[-1])

    def start_worker(self):
        process = subprocess.Popen([sys.executable, '-B', str(Path(__file__).resolve()), '--worker', str(self.root)],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(lambda: (process.kill(), process.wait()) if process.poll() is None else None)
        deadline = time.monotonic() + 5
        while not (self.root / 'ready').exists():
            if process.poll() is not None:
                self.fail(str(process.communicate()))
            if time.monotonic() > deadline:
                self.fail('worker did not reach staging')
            time.sleep(0.01)
        return process

    def test_concurrent_installer(self):
        process = self.start_worker()
        code, output = self.invoke()
        self.assertEqual(75, code)
        self.assertIn('holds the lifecycle lock', output)
        (self.root / 'release').touch()
        process.communicate(timeout=10)
        self.assertEqual(0, process.returncode)
        self.assertEqual(3, len(self.calls()))

    def test_sigterm_during_staging_and_retry(self):
        process = self.start_worker()
        process.terminate()
        process.communicate(timeout=5)
        self.assertEqual(143, process.returncode)
        self.assertEqual([], list((self.root / 'sources').glob('*.stage-*')))
        self.assertFalse(self.destination().exists())
        self.assertEqual([], self.calls())
        self.assertEqual(0, self.invoke()[0])

    def test_sigkill_stale_staging_preserved(self):
        process = self.start_worker()
        process.kill()
        process.communicate(timeout=5)
        stale = list((self.root / 'sources').glob('*.stage-*'))
        self.assertEqual(1, len(stale))
        self.assertEqual(0o700, stale[0].stat().st_mode & 0o777)
        # Stale state is neither reused nor purged by the next invocation.
        with redirect_stdout(io.StringIO()):
            installer.install(self.args)
        self.assertTrue(stale[0].exists())
        self.assertTrue(self.destination().exists())

    def test_lock_symlink_rejected(self):
        installer.LOCK_ROOT.mkdir(mode=0o700)
        (installer.LOCK_ROOT / 'install.lock').symlink_to(self.root / 'absent')
        self.rejected()

    def test_missing_kbuild_executable(self):
        (Path(self.args.headers) / 'scripts/mod/modpost').chmod(0o600)
        self.rejected('Kbuild prerequisite')

    def test_empty_source_directory(self):
        (self.root / 'input/includes/public.h').unlink()
        self.rejected('empty source directory')

    def test_stage_mkdir_failure(self):
        original = installer.os.mkdir
        def fail(path, *args, **kwargs):
            if str(path).startswith('.hailo_pci-'):
                raise OSError('injected stage allocation failure')
            return original(path, *args, **kwargs)
        with patch.object(installer.os, 'mkdir', side_effect=fail):
            self.rejected('stage allocation failure')
        self.assertEqual(0, self.invoke()[0])

    def test_registration_appears_during_staging(self):
        original = installer.copy_tree
        def collide(fd, target):
            original(fd, target)
            (self.root / 'dkms/hailo_pci/4.24.0').mkdir(exist_ok=True)
        with patch.object(installer, 'copy_tree', side_effect=collide):
            self.rejected('already exists')
        self.assertTrue((self.root / 'dkms/hailo_pci/4.24.0').exists())

    def test_unsafe_dkms_module_parent(self):
        (self.root / 'dkms/hailo_pci').chmod(0o777)
        self.rejected('unsafe DKMS module directory')

    def test_lock_directory_symlink(self):
        installer.LOCK_ROOT.symlink_to(self.root / 'input')
        self.rejected()

    def test_lock_hardlink_rejected(self):
        installer.LOCK_ROOT.mkdir(mode=0o700)
        file = self.root / 'unrelated'
        file.write_text('unchanged')
        file.chmod(0o600)
        os.link(file, installer.LOCK_ROOT / 'install.lock')
        self.rejected('unsafe lifecycle lock file')
        self.assertEqual('unchanged', file.read_text())

    def test_interruption_immediately_after_publication(self):
        original = installer.publish
        def interrupt(stage, root, name):
            original(stage, root, name)
            os.kill(os.getpid(), signal.SIGTERM)
        previous = signal.signal(signal.SIGTERM, installer.interrupted)
        try:
            with patch.object(installer, 'publish', side_effect=interrupt), redirect_stderr(io.StringIO()) as output:
                with self.assertRaises(SystemExit) as error:
                    installer.install(self.args)
            self.assertEqual(143, error.exception.code)
            self.assertIn('published sources and transcript retained', output.getvalue())
        finally:
            signal.signal(signal.SIGTERM, previous)
        self.assertTrue(self.destination().exists())
        self.assertEqual([], self.calls())
        self.assertEqual([], list((self.root / 'sources').glob('*.stage-*')))

    def test_cleanup_refuses_replaced_staging_path(self):
        def replace(fd, target):
            stages = list((self.root / 'sources').glob('*.stage-*'))
            stage = stages[0]
            stage.rename(self.root / 'retained-stage')
            stage.mkdir()
            (stage / 'unowned').write_text('keep')
            raise OSError('interrupted')
        with patch.object(installer, 'copy_tree', side_effect=replace):
            with self.assertRaisesRegex(installer.InstallError, 'staging path replaced'):
                installer.install(self.args)
        stage = next((self.root / 'sources').glob('*.stage-*'))
        self.assertEqual('keep', (stage / 'unowned').read_text())
        self.assertTrue((self.root / 'retained-stage').exists())
        self.assertEqual([], self.calls())

    def test_sigterm_during_dkms_retains_state_and_stops_child(self):
        with patch.dict(os.environ, INSTALL_HOLD='build'):
            process = self.start_worker()
        (self.root / 'release').touch()
        deadline = time.monotonic() + 5
        while not (self.root / 'dkms-ready').exists():
            self.assertIsNone(process.poll())
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.01)
        child_pid = int((self.root / 'dkms-ready').read_text())
        process.terminate()
        _, error = process.communicate(timeout=5)
        self.assertEqual(143, process.returncode)
        self.assertIn('Stopped during dkms build', error)
        self.assertTrue(self.destination().exists())
        self.assertEqual(2, len(self.calls()))
        with self.assertRaises(ProcessLookupError):
            os.kill(child_pid, 0)
        self.assertEqual([], list((self.root / 'sources').glob('*.stage-*')))
        before = self.calls()
        self.assertNotEqual(0, self.invoke()[0])
        self.assertEqual(before, self.calls())

    def test_dkms_spawn_failure_retains_transcript(self):
        with patch.object(installer.subprocess, 'Popen', side_effect=OSError('injected spawn failure')):
            code, output = self.invoke()
        self.assertNotEqual(0, code)
        self.assertIn('injected spawn failure', output)
        self.assertIn('Stopped during dkms add', output)
        self.assertTrue((self.destination() / '.hailo-install.log').exists())
        self.assertEqual([], self.calls())

    def test_published_tree_replacement_stops_dkms(self):
        original = installer.publish
        def replace(stage, root, name):
            original(stage, root, name)
            self.destination().rename(self.root / 'retained-published-tree')
            self.destination().symlink_to(self.root / 'absent')
        with patch.object(installer, 'publish', side_effect=replace):
            code, output = self.invoke()
        self.assertNotEqual(0, code)
        self.assertIn('published source path replaced', output)
        self.assertTrue(self.destination().is_symlink())
        self.assertTrue((self.root / 'retained-published-tree/.hailo-install.log').exists())
        self.assertEqual([], self.calls())

    def test_registration_race_after_publication(self):
        original = installer.publish
        def collide(stage, root, name):
            original(stage, root, name)
            (self.root / 'dkms/hailo_pci/4.24.0').mkdir()
        with patch.object(installer, 'publish', side_effect=collide):
            code, output = self.invoke()
        self.assertNotEqual(0, code)
        self.assertIn('already exists', output)
        self.assertTrue(self.destination().exists())
        self.assertEqual([], self.calls())

    def test_configuration_generation_failure(self):
        original = Path.write_text
        def fail(path, *args, **kwargs):
            if path.name == 'dkms.conf':
                raise OSError('injected configuration generation failure')
            return original(path, *args, **kwargs)
        with patch.object(Path, 'write_text', new=fail):
            self.rejected('configuration generation failure')

    def test_template_fifo_race_is_nonblocking(self):
        original = installer.manifest
        def race(fd, prefix=''):
            entries = original(fd, prefix)
            if 'public.h' in entries:
                path = self.root / 'input/pcie/dkms.conf.in'
                path.unlink()
                os.mkfifo(path)
            return entries
        with patch.object(installer, 'manifest', side_effect=race):
            self.rejected('template changed type')

    def test_transcript_failure_does_not_mask_dkms_status_or_cleanup(self):
        def fail(command, log):
            log.close()  # Model a lost/unwritable diagnostic stream.
            raise installer.InstallError('modeled DKMS failure', 17)
        with patch.object(installer, 'run_dkms', side_effect=fail):
            code, output = self.invoke()
        self.assertEqual(17, code)
        self.assertIn('Transcript finalization failed', output)
        self.assertIn('No rollback attempted', output)
        self.assertTrue(self.destination().exists())

    def descendant_case(self, interrupted, detached=False):
        with patch.dict(os.environ, INSTALL_DESCENDANT='detached' if detached else 'same-group', INSTALL_HOLD='build' if interrupted else ''):
            process = self.start_worker()
        (self.root / 'release').touch()
        if interrupted:
            deadline = time.monotonic() + 5
            while not (self.root / 'dkms-ready').exists():
                self.assertIsNone(process.poll())
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.01)
            process.terminate()
        process.communicate(timeout=6)
        self.assertEqual(143 if interrupted else 0, process.returncode)
        result = json.loads((self.root / 'descendant-result').read_text())
        self.assertEqual(detached, result['survived'], 'original-group descendants must terminate; detached hooks are outside the guarantee')

    def test_descendant_cannot_survive_interrupted_dkms_group(self):
        self.descendant_case(interrupted=True)

    def test_descendant_cannot_survive_normal_dkms_group(self):
        self.descendant_case(interrupted=False)

    def test_detached_descendant_is_explicitly_outside_group_guarantee(self):
        self.descendant_case(interrupted=True, detached=True)

    def test_python310_api_surface(self):
        ast.parse((ROOT / 'linux/pcie/dkms_install.py').read_text(), feature_version=(3, 10))
        original = installer.shutil.rmtree
        def legacy_rmtree(path):  # Python 3.10: no dir_fd keyword.
            return original(path)
        legacy_rmtree.avoids_symlink_attacks = True
        with patch.object(installer.shutil, 'rmtree', new=legacy_rmtree), patch.object(installer.hashlib, 'file_digest', side_effect=AssertionError('3.11 API called'), create=True):
            self.assertEqual(0, self.invoke()[0])

    def test_chunked_hashes_nonblocking_regular_files(self):
        for data in (b'', b'a', b'chunk' * 500000):
            path = self.root / 'hash-input'
            path.write_bytes(data)
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, 'rb') as stream:
                self.assertEqual(hashlib.sha256(data).hexdigest(), installer.hash_stream(stream))

    def test_hash_would_block_never_returns_digest(self):
        from unittest.mock import Mock
        with self.assertRaises(BlockingIOError):
            installer.hash_stream(Mock(read=Mock(return_value=None)))
        with self.assertRaises(BlockingIOError):
            installer.hash_stream(Mock(read=Mock(side_effect=BlockingIOError('would block'))))

    def test_group_signals_precede_direct_child_reap(self):
        from unittest.mock import Mock
        events = []
        child = Mock(pid=12345)
        child.wait.side_effect = lambda **kw: events.append(('wait', kw)) or 0
        with patch.object(installer.os, 'waitid', return_value=None), patch.object(installer.os, 'killpg', side_effect=lambda pid, sig: events.append(('signal', pid, sig))):
            installer.stop_dkms_group(child, grace=0)
        self.assertEqual([('signal', 12345, signal.SIGTERM), ('signal', 12345, signal.SIGKILL), ('wait', {'timeout': 2})], events)

    def test_group_cleanup_timeout_is_bounded_and_reported(self):
        from unittest.mock import Mock
        child = Mock(pid=12345)
        child.wait.side_effect = subprocess.TimeoutExpired('fixture', 2)
        with patch.object(installer.os, 'waitid', return_value=None), patch.object(installer.os, 'killpg') as group:
            with self.assertRaisesRegex(installer.InstallError, 'tasks may remain'):
                installer.stop_dkms_group(child, grace=0)
        child.wait.assert_called_once_with(timeout=2)
        self.assertEqual(2, group.call_count)

    def test_reaped_leader_never_receives_numeric_group_signals(self):
        from unittest.mock import Mock
        child = Mock(pid=12345)
        with patch.object(installer.os, 'waitid', side_effect=ChildProcessError), patch.object(installer.os, 'killpg') as group:
            with self.assertRaisesRegex(installer.InstallError, 'refusing numeric group signals'):
                installer.stop_dkms_group(child, grace=0)
        group.assert_not_called()
        child.wait.assert_not_called()

    def test_automatic_reaping_rejected_before_mutation(self):
        with patch.object(installer.signal, 'getsignal', return_value=signal.SIG_IGN):
            self.rejected('default SIGCHLD disposition')

    def test_make_arguments_are_literal(self):
        marker = self.root / 'must-not-exist'
        value = "bad'; touch " + str(marker) + "; echo '"
        result = subprocess.run(['make', '-C', str(ROOT / 'linux/pcie'), 'install_dkms', 'DRIVER_VERSION=' + value],
                                capture_output=True, text=True, timeout=5)
        self.assertNotEqual(0, result.returncode)
        self.assertIn('invalid driver version', result.stderr)
        self.assertFalse(marker.exists())


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--worker':
        worker(Path(sys.argv[2]))
    else:
        unittest.main()
