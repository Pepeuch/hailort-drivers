"""Test command: touches only a confined fixture, never invokes real DKMS."""
import os
from pathlib import Path
import sys

root = Path(os.environ['INSTALL_FIXTURE']).resolve()
allowed = Path(__file__).resolve().parents[2] / '.workbench/tmp'
if root == allowed or not root.is_relative_to(allowed):
    raise ValueError('fixture outside workbench')
action, *args = sys.argv[1:]
assert action in ('add', 'build', 'install')
assert args[:2] == ['-m', 'hailo_pci']
assert args[args.index('--sourcetree') + 1] == str(root / 'sources')
assert args[args.index('--dkmstree') + 1] == str(root / 'dkms')
assert '--force' not in args
version = args[args.index('-v') + 1]
assert version in ('4.24.0', '4.25.0')
with (root / 'calls').open('a') as stream:
    stream.write(' '.join([action, *args]) + '\n')
state = root / 'dkms/hailo_pci' / version
state.mkdir(parents=True, exist_ok=True)
(state / (action + '.attempted')).write_text('partial modeled DKMS state')
print('diagnostic from mocked ' + action, flush=True)
if action == 'build' and os.environ.get('INSTALL_DESCENDANT'):
    import signal
    import time
    pid = os.fork()
    if pid == 0:
        if os.environ.get('INSTALL_DESCENDANT') == 'detached':
            os.setsid()
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        (root / 'descendant-ready').write_text(str(os.getpid()))
        while True:
            signal.pause()
    deadline = time.monotonic() + 3
    while not (root / 'descendant-ready').exists():
        if time.monotonic() > deadline:
            raise RuntimeError('descendant startup timeout')
        time.sleep(0.01)
if os.environ.get('INSTALL_HOLD') == action:
    import time
    (root / 'dkms-ready').write_text(str(os.getpid()))
    while not (root / 'dkms-release').exists():
        time.sleep(0.01)
if os.environ.get('INSTALL_FAIL') == action:
    mode = os.environ.get('INSTALL_LOG', 'normal')
    if mode != 'missing':
        log = state / 'build/make.log'
        log.parent.mkdir(exist_ok=True)
        log.write_text('preserved build diagnostic')
        if mode == 'unreadable':
            log.chmod(0)
    raise SystemExit(17)
if action != 'add':
    kernel = args[args.index('-k') + 1]
    assert kernel in ('kernel-a', 'kernel-b')
    (state / (action + '.' + kernel)).write_text('modeled success')
