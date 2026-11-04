#!/usr/bin/env python3
"""Linux-only, no-replace DKMS source preparation and explicit recovery."""

import argparse
from contextlib import ExitStack, contextmanager
import ctypes
import fcntl
import hashlib
import os
from pathlib import Path
import re
import secrets
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import time

SOURCE_ROOT = Path('/usr/src')
DKMS_ROOT = Path('/var/lib/dkms')
LOCK_ROOT = Path('/run/hailo-dkms-installer')
NAME = 'hailo_pci'
DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
SIGNALS = {signal.SIGTERM, signal.SIGINT, signal.SIGHUP}


class InstallError(Exception):
    def __init__(self, message, code=1):
        super().__init__(message)
        self.code = code


def require_root():
    if os.geteuid() != 0:
        raise InstallError('make install_dkms should run as root')


@contextmanager
def directory(path, trusted=False):
    """Open each component without following links; pin the resulting inode."""
    path = Path(os.path.abspath(path))
    fd = os.open('/', DIRECTORY)
    try:
        for part in path.parts[1:]:
            new = os.open(part, DIRECTORY, dir_fd=fd)
            os.close(fd)
            fd = new
            if trusted:
                info = os.fstat(fd)
                if info.st_uid not in (0, os.geteuid()) or info.st_mode & 0o022:
                    raise InstallError(f'unsafe destination ancestor: {path}')
        yield fd
    finally:
        os.close(fd)


def check_absent(parent, name):
    try:
        os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return
    raise InstallError(f'{name} already exists (including symlinks); inspect retained state; refusing to overwrite')


def check_registration(dkms_fd, version):
    try:
        child = os.open(NAME, DIRECTORY, dir_fd=dkms_fd)
    except FileNotFoundError:
        return
    try:
        info = os.fstat(child)
        if info.st_uid not in (0, os.geteuid()) or info.st_mode & 0o022:
            raise InstallError('unsafe DKMS module directory')
        check_absent(child, version)
    finally:
        os.close(child)


def unchanged(path, fd):
    with directory(path, trusted=True) as current:
        a, b = os.fstat(current), os.fstat(fd)
        if (a.st_dev, a.st_ino) != (b.st_dev, b.st_ino):
            raise InstallError(f'destination parent replaced during installation: {path}')


def hash_stream(stream):
    """Bounded-memory SHA-256, including on Python 3.10; never hash None."""
    digest = hashlib.sha256()
    while True:
        chunk = stream.read(1024 * 1024)
        if chunk is None:
            raise BlockingIOError('source read would block')
        if not chunk:
            return digest.hexdigest()
        digest.update(chunk)


def manifest(fd, prefix=''):
    """Read regular files using no-follow opens; reject special files and links."""
    result = {}
    for name in sorted(os.listdir(fd)):
        relative = prefix + name
        info = os.stat(name, dir_fd=fd, follow_symlinks=False)
        if stat.S_ISDIR(info.st_mode):
            child = os.open(name, DIRECTORY, dir_fd=fd)
            try:
                result[relative + '/'] = None
                result.update(manifest(child, relative + '/'))
            finally:
                os.close(child)
        elif stat.S_ISREG(info.st_mode):
            file_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
            with os.fdopen(file_fd, 'rb') as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise InstallError(f'source changed type: {relative}')
                result[relative] = hash_stream(stream)
        else:
            raise InstallError(f'unsupported source entry (symlink/special file): {relative}')
    return result


def copy_tree(source_fd, target):
    """Copy through pinned directories, never following source symlinks."""
    target.mkdir()
    for name in sorted(os.listdir(source_fd)):
        info = os.stat(name, dir_fd=source_fd, follow_symlinks=False)
        if stat.S_ISDIR(info.st_mode):
            child = os.open(name, DIRECTORY, dir_fd=source_fd)
            try:
                copy_tree(child, target / name)
            finally:
                os.close(child)
        elif stat.S_ISREG(info.st_mode):
            file_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=source_fd)
            with os.fdopen(file_fd, 'rb') as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise InstallError(f'source changed type: {name}')
                with (target / name).open('xb') as output:
                    shutil.copyfileobj(stream, output)
            # Do not propagate setuid/setgid/world-writable permissions.
            (target / name).chmod(0o755 if info.st_mode & 0o111 else 0o644)
        else:
            raise InstallError(f'unsupported source entry: {name}')


def publish(stage_fd, root_fd, name):
    libc = ctypes.CDLL(None, use_errno=True)
    rename = libc.renameat2
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(stage_fd, b'source', root_fd, os.fsencode(name), 1):
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), name)


def stop_dkms_group(child, grace):
    """Signal only while the unreaped session leader pins the group identity.

    No wait()/poll() may precede this call. This is bounded group signalling,
    not containment of hooks that detach or proof that all tasks have exited.
    """
    old_mask = signal.pthread_sigmask(signal.SIG_BLOCK, SIGNALS)
    try:
        try:
            os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOWAIT | os.WNOHANG)
        except ChildProcessError as error:
            raise InstallError('DKMS leader is no longer our waitable child; refusing numeric group signals') from error
        for sig, delay in ((signal.SIGTERM, grace), (signal.SIGKILL, 0)):
            try:
                os.killpg(child.pid, sig)
            except ProcessLookupError:
                pass
            if delay:
                time.sleep(delay)
        try:
            return child.wait(timeout=2)
        except subprocess.TimeoutExpired as error:
            raise InstallError('DKMS leader did not exit after group termination; tasks may remain; inspect before any retry') from error
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, old_mask)


def run_dkms(command, log):
    print('Running: ' + shlex.join(command), flush=True)
    log.write('Running: ' + shlex.join(command) + '\n')
    log.flush()
    # Defer handled signals across Popen + bookkeeping without blocking them
    # in the child process. The helper is single-threaded.
    pending = []
    handlers = {sig: signal.signal(sig, lambda number, _frame: pending.append(number)) for sig in SIGNALS}
    child = None
    try:
        try:
            child = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        finally:
            for sig, handler in handlers.items():
                signal.signal(sig, handler)
        if pending:
            interrupted(pending[0], None)
        # Observe exit without reaping. A zombie leader still pins its numeric
        # PID/PGID, so group signals cannot target a reused unrelated group.
        while os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOWAIT | os.WNOHANG) is None:
            time.sleep(0.01)
    except BaseException:
        if child is not None:
            stop_dkms_group(child, grace=0.25)
        raise
    # Hooks must be synchronous. Stop lingering original-group descendants on
    # success/failure too, while the completed direct child remains unreaped.
    code = stop_dkms_group(child, grace=0)
    if code:
        raise InstallError(f'DKMS failed with status {code}', code if code > 0 else 128 - code)


def recovery(destination, version, kernel, headers, phase):
    print(f'Stopped during {phase}; published sources and transcript retained: {destination}', file=sys.stderr)
    print(f'Inspect {destination}/.hailo-install.log and dkms status -m {NAME} -v {version}.', file=sys.stderr)
    print(f'DKMS build logs, if present/readable: {DKMS_ROOT / NAME / version}/build/make.log or <kernel>/<arch>/log/make.log.', file=sys.stderr)
    print('No rollback attempted. Resolve the cause, inspect registration/build/install state, then run only the first incomplete step:', file=sys.stderr)
    for action in ('add', 'build', 'install'):
        command = ['dkms', action, '-m', NAME, '-v', version, '--sourcetree', str(SOURCE_ROOT), '--dkmstree', str(DKMS_ROOT)]
        if action != 'add':
            command += ['-k', kernel, '--kernelsourcedir', headers]
        print('  ' + shlex.join(command), file=sys.stderr)
    print('Do not rerun this installer over retained sources; do not purge backups/other versions or use --force. See Readme.md for recovery.', file=sys.stderr)


def preflight_headers(headers, kernel):
    if not headers.is_absolute():
        raise InstallError('kernel header path must be absolute')
    for name in ('Makefile', 'Module.symvers', 'include/config/auto.conf',
                 'include/generated/autoconf.h', 'include/generated/utsrelease.h'):
        path = headers / name
        if not path.is_file() or not os.access(path, os.R_OK):
            raise InstallError(f'missing/unreadable prepared kernel header: {path}')
    for name in ('scripts/basic/fixdep', 'scripts/mod/modpost'):
        if not os.access(headers / name, os.X_OK):
            raise InstallError(f'missing executable Kbuild prerequisite: {headers / name}')
    if (headers / 'include/generated/utsrelease.h').read_text().strip() != f'#define UTS_RELEASE "{kernel}"':
        raise InstallError('prepared headers do not match selected kernel release')


def install(args):
    if not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', args.version):
        raise InstallError('invalid driver version', 2)
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._+\-]*', args.kernel):
        raise InstallError('invalid kernel release', 2)
    require_root()
    if signal.getsignal(signal.SIGCHLD) != signal.SIG_DFL:
        raise InstallError('default SIGCHLD disposition required; automatic/custom reaping is unsupported')
    for tool in ('dkms', 'make', 'gcc', 'ld', 'as', 'ar', 'nm', 'objcopy', 'objdump', 'strip', 'depmod'):
        if shutil.which(tool) is None:
            raise InstallError(f'required tool missing: {tool}')
    if not hasattr(os, 'waitid') or not hasattr(os, 'WNOWAIT'):
        raise InstallError('Linux waitid/WNOWAIT are required for safe process-group signalling')
    if not hasattr(ctypes.CDLL(None), 'renameat2') or not shutil.rmtree.avoids_symlink_attacks:
        raise InstallError('renameat2 and descriptor-safe cleanup are required; no unsafe fallback')
    preflight_headers(Path(args.headers), args.kernel)
    sources = [(args.pcie, 'hailort/drivers/linux/pcie'),
               (args.common, 'hailort/drivers/common'), (args.vdma, 'hailort/drivers/linux/vdma'),
               (args.utils, 'hailort/drivers/linux/utils'), (args.includes, 'common/include')]
    name = f'{NAME}-{args.version}'
    destination = SOURCE_ROOT / name
    with ExitStack() as stack:
        inputs = []
        for path, relative in sources:
            try:
                fd = stack.enter_context(directory(path))
            except OSError as error:
                # The standalone repository has no SDK common/include tree.
                # Only its absence is optional; unsafe or malformed inputs fail.
                if relative == 'common/include' and isinstance(error, FileNotFoundError):
                    continue
                raise InstallError(f'missing/unsafe source directory: {path}') from error
            entries = manifest(fd)
            if not entries:
                raise InstallError(f'empty source directory: {path}')
            inputs.append((fd, relative, entries))
        required = {0: ('Makefile', 'Kbuild', 'dkms.conf.in', 'src/pcie.c', 'src/fops.c',
                        'src/sysfs.c', 'src/nnc.c', 'src/soc.c'),
                    1: ('hailo_ioctl_common.h', 'fw_validation.c', 'fw_operation.c',
                        'pcie_common.c', 'vdma_common.c', 'hailo_resource.c'),
                    2: ('vdma.c', 'memory.c', 'ioctl.c'), 3: ('logs.c',)}
        for index, paths in required.items():
            for path in paths:
                if inputs[index][2].get(path) is None:
                    raise InstallError(f'missing required source file: {sources[index][0]}/{path}')
        template_fd = os.open('dkms.conf.in', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=inputs[0][0])
        with os.fdopen(template_fd, 'rb') as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise InstallError('DKMS configuration template changed type')
            data = stream.read()
        if hashlib.sha256(data).hexdigest() != inputs[0][2]['dkms.conf.in']:
            raise InstallError('DKMS configuration template changed during preflight')
        try:
            template = data.decode('utf-8')
        except UnicodeDecodeError as error:
            raise InstallError('invalid DKMS configuration template encoding') from error
        if template.count('@PCIE_DRIVER_VERSION@') != 1:
            raise InstallError('invalid DKMS configuration template')
        src_fd = stack.enter_context(directory(SOURCE_ROOT, trusted=True))
        dkms_fd = stack.enter_context(directory(DKMS_ROOT, trusted=True))
        check_absent(src_fd, name)
        check_registration(dkms_fd, args.version)
        if not os.access(SOURCE_ROOT, os.W_OK | os.X_OK):
            raise InstallError('installation destination is not writable')
        # Reject an obviously full filesystem; later ENOSPC is still handled.
        space = os.fstatvfs(src_fd)
        if not space.f_bavail or not space.f_favail:
            raise InstallError('installation destination has no available blocks/inodes')
        with directory(LOCK_ROOT.parent, trusted=True) as lock_parent:
            try:
                os.mkdir(LOCK_ROOT.name, 0o700, dir_fd=lock_parent)
            except FileExistsError:
                pass
        lock_dir = stack.enter_context(directory(LOCK_ROOT, trusted=True))
        if stat.S_IMODE(os.fstat(lock_dir).st_mode) != 0o700:
            raise InstallError('unsafe lifecycle lock directory mode')
        lock = os.open('install.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                       0o600, dir_fd=lock_dir)
        stack.callback(os.close, lock)
        info = os.fstat(lock)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1 or info.st_mode & 0o077:
            raise InstallError('unsafe lifecycle lock file')
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise InstallError('another installer holds the lifecycle lock; retry after it finishes', 75) from None
        check_absent(src_fd, name)
        check_registration(dkms_fd, args.version)
        stage_name = f'.{name}.stage-{secrets.token_hex(16)}'
        stage_fd = None
        tree_fd = None
        published = False
        phase = 'staging'
        log = None
        # Prevent an interruption between creating a resource and recording it.
        old_mask = signal.pthread_sigmask(signal.SIG_BLOCK, SIGNALS)
        try:
            os.mkdir(stage_name, 0o700, dir_fd=src_fd)
            stage_fd = os.open(stage_name, DIRECTORY, dir_fd=src_fd)
        except BaseException:
            signal.pthread_sigmask(signal.SIG_SETMASK, old_mask)
            raise
        try:
            signal.pthread_sigmask(signal.SIG_SETMASK, old_mask)
            stage = Path(f'/proc/self/fd/{stage_fd}')
            tree = stage / 'source'
            tree.mkdir(mode=0o755)
            for fd, relative, entries in inputs:
                target = tree / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                copy_tree(fd, target)
                target_fd = os.open(target, DIRECTORY)
                try:
                    if manifest(target_fd) != entries or manifest(fd) != entries:
                        raise InstallError(f'source changed or staged copy incomplete: {relative}')
                finally:
                    os.close(target_fd)
            (tree / 'dkms.conf').write_text(template.replace('@PCIE_DRIVER_VERSION@', args.version))
            log = os.fdopen(os.open(tree / '.hailo-install.log', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w')
            log.write(f'{NAME}/{args.version}, kernel {args.kernel}: prepared; no DKMS command attempted yet\n')
            log.flush()
            unchanged(SOURCE_ROOT, src_fd)
            unchanged(DKMS_ROOT, dkms_fd)
            check_registration(dkms_fd, args.version)
            tree_fd = os.open('source', DIRECTORY, dir_fd=stage_fd)
            phase = 'publication'
            old_mask = signal.pthread_sigmask(signal.SIG_BLOCK, SIGNALS)
            try:
                publish(stage_fd, src_fd, name)
                published = True
            finally:
                signal.pthread_sigmask(signal.SIG_SETMASK, old_mask)
            for action in ('add', 'build', 'install'):
                unchanged(SOURCE_ROOT, src_fd)
                unchanged(DKMS_ROOT, dkms_fd)
                current = os.stat(name, dir_fd=src_fd, follow_symlinks=False)
                expected = os.fstat(tree_fd)
                if not stat.S_ISDIR(current.st_mode) or (current.st_dev, current.st_ino) != (expected.st_dev, expected.st_ino):
                    raise InstallError('published source path replaced; refusing further DKMS commands')
                if action == 'add':
                    check_registration(dkms_fd, args.version)
                phase = f'dkms {action}'
                command = ['dkms', action, '-m', NAME, '-v', args.version,
                           '--sourcetree', str(SOURCE_ROOT), '--dkmstree', str(DKMS_ROOT)]
                if action != 'add':
                    command += ['-k', args.kernel, '--kernelsourcedir', args.headers]
                run_dkms(command, log)
                log.write(f'{phase}: complete\n')
                log.flush()
            phase = 'complete'
            print(f'Installed {NAME}/{args.version} for {args.kernel}; transcript: {destination}/.hailo-install.log')
        finally:
            # Cleanup cannot be interrupted into releasing the lock prematurely.
            signal.pthread_sigmask(signal.SIG_BLOCK, SIGNALS)
            try:
                if log is not None:
                    try:
                        log.write(f'Installer stopped at: {phase}\n')
                        log.close()
                    except (OSError, ValueError) as error:
                        print(f'Transcript finalization failed: {error}; inspect retained state.', file=sys.stderr)
                        try:
                            log.close()
                        except OSError:
                            pass
                if published and phase != 'complete':
                    recovery(destination, args.version, args.kernel, args.headers, phase)
                if stage_fd is not None:
                    current = os.stat(stage_name, dir_fd=src_fd, follow_symlinks=False)
                    owned = os.fstat(stage_fd)
                    if (current.st_dev, current.st_ino) != (owned.st_dev, owned.st_ino):
                        raise InstallError('staging path replaced; retained for manual inspection')
                    # Python 3.10 lacks rmtree(dir_fd=...). The pinned /proc fd
                    # path retains identical parent anchoring with fd-safe rmtree.
                    shutil.rmtree(Path(f'/proc/self/fd/{src_fd}') / stage_name)
            finally:
                if tree_fd is not None:
                    os.close(tree_fd)
                if stage_fd is not None:
                    os.close(stage_fd)
                signal.pthread_sigmask(signal.SIG_SETMASK, old_mask)


def interrupted(signum, _frame):
    raise SystemExit(128 + signum)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('version', 'kernel', 'headers', 'pcie', 'common', 'vdma', 'utils', 'includes'):
        parser.add_argument('--' + name, required=True)
    args = parser.parse_args()
    for sig in SIGNALS:
        signal.signal(sig, interrupted)
    try:
        install(args)
    except (InstallError, OSError) as error:
        print(f'DKMS installation failed: {error}', file=sys.stderr)
        return error.code if isinstance(error, InstallError) else 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
