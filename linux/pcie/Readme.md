This project contains the source code of the Hailo PCIe driver for Linux.

## Build the driver.
Run the command `make all`. The driver will be compiled with the current running
kernel, in order to cross compile, the user needs to set the KERNEL_DIR environment variable.
 
## Install/Uninstall the driver

`sudo make install` installs a standalone module using Kbuild.
`sudo make install_dkms` registers, builds and installs the current driver
version through DKMS. It never implicitly uninstalls an existing version and
does not use `--force`. An existing registration or source tree is refused.
DKMS build and install select `kernelver` (the running kernel by default).

Removal is explicitly separated by installation type:

| Target | Removal scope |
| --- | --- |
| `sudo make uninstall` | Alias for `uninstall_dkms`: this driver version, selected `kernelver` only. |
| `sudo make uninstall_dkms kernelver=KERNEL_RELEASE` | This DKMS driver version for that kernel; DKMS owns file removal, original-module restoration and depmod. |
| `sudo make uninstall_all_dkms` | This DKMS driver version for all kernels; never other driver versions. |
| `sudo make uninstall_standalone` | Fails closed without deleting files: legacy standalone installations have no reliable installer ownership records. |

The old `uninstall` target could remove both installation types; it no longer
falls back to standalone cleanup. Automatic removal after a non-DKMS
`make install` is unsupported, as explained below. An absent DKMS registration is reported by
DKMS as an error, rather than being treated as permission to delete a module.
The restored `uninstall_all_dkms` target now means all kernels of the selected
version, not all installed driver versions. Direct `sh dkms_uninstall.sh VERSION`
retains its version-scoped all-kernels interface; add `--kernel KERNEL_RELEASE`
to select one kernel.

Matching module name, version, kernel metadata, pathname or even byte content
does not establish which installer owns that file. Absence of a DKMS entry is
not positive evidence of standalone ownership either. No automatic standalone
cleanup is provided until installation records reliable provenance. The refusal
also covers same-version originals restored by DKMS, malformed/unreadable files,
compressed modules and symbolic links. No metadata parsing or filesystem deletion
is attempted by the standalone target.

After a non-DKMS installation, retain the module and inspect its installer or
package-manager provenance; `uninstall_standalone` does not offer a force flag or
turn version matching into authorization to delete it. Even an already absent
standalone installation returns the unsupported-operation error. Normal DKMS
removal does not unload the running module. Serialize lifecycle actions; these
recipes do not provide a lock against concurrent package/DKMS changes.

### Failure-safe DKMS source preparation

`install_dkms` delegates to `dkms_install.py`. This Linux-only helper requires
Python 3.10 or newer, `/proc/self/fd`, libc/kernel/filesystem support for
`renameat2(RENAME_NOREPLACE)`, and descriptor-safe Python filesystem cleanup.
There is no replacement-rename fallback. It targets the standard `/usr/src` and
`/var/lib/dkms` trees explicitly, including when DKMS configuration specifies
other defaults. Nonstandard tree layouts and symlinked destination ancestors
are unsupported. Header paths may use the normal packaged-header symlinks.

Preflight checks version/kernel syntax, root, build commands, prepared headers
(including matching UTS release, Module.symvers, fixdep and modpost), source
inputs, destination ancestors, existing source/registration paths and available
blocks/inodes. Headers are selected by `KERNEL_DIR` and `kernelver`, for example:

```sh
sudo make install_dkms kernelver=6.12.113+deb13-amd64 \
  KERNEL_DIR=/usr/src/linux-headers-6.12.113+deb13-amd64
```

The SDK common include directory is still required. In a standalone checkout,
its absence fails before staging; the separate source-layout fix in PR #56 is
not implemented here. Do not create dummy includes to bypass this check. Empty
source directories, symlinks and special files in source trees are rejected.
Sources and build tools must be trusted; this is not a sandbox for untrusted
root-executed Makefiles, DKMS configuration, hooks or compiler inputs. GNU Make
itself interprets command-line Make expressions; only ordinary value arguments
are intended. The helper also validates direct invocation arguments.

Our installer uses a persistent advisory lock inode in
`/run/hailo-dkms-installer/install.lock`, protected by a root-owned 0700 directory.
A competing invocation exits 75 without staging or running DKMS. Never unlink
the lock file as a way to unlock it. This lock serializes our installers across
versions/kernels; removal targets, arbitrary package managers, external DKMS
commands and manual filesystem changes do not automatically honor it. Serialize
those operations administratively. Root-level races cannot be fully prevented
by an advisory lock or identity checks.

Private staging directories are created with mode 0700 in `/usr/src`, on the
publication filesystem. Sources are copied through pinned, no-follow directory
and file descriptors. Manifests check the full copied contents against preflight
hashes and reject source changes. The helper generates `dkms.conf` and a private
0600 `.hailo-install.log`, then publishes the completed tree atomically without
replacing any existing file, directory or dangling symlink. Parent and published
tree identity checks guard subsequent DKMS calls. No prior version is removed.
Preflight is not a disk-space reservation or a guarantee that compilation will
succeed; later I/O, toolchain or kernel-configuration errors remain possible.

| Failure/stop stage | Retained state | Recovery |
| --- | --- | --- |
| Preflight, existing destination or registration | Existing files/state unchanged; no source staging or DKMS action | Correct prerequisites or inspect existing state. Never delete a collision simply to make retry succeed. |
| Lock contention | Existing state unchanged; no staging/DKMS action | Wait for the other installer; retry. |
| Stage allocation/copy/validation/config generation | Only this invocation's verified private staging directory is cleaned; persistent lock infrastructure may remain | Correct cause and rerun installer. Cleanup errors retain staging for inspection. |
| No-replace publication fails | Conflicting destination preserved; owned private staging cleaned | Inspect collision/filesystem support, then retry only when appropriate. |
| After publication, before/during `dkms add` | Complete source tree/transcript retained; registration may be absent or partial | Inspect status and transcript; run `dkms add` only if registration is absent and sources are intact. |
| `dkms build` fails | Sources, registration, transcript and any DKMS logs/build state retained | Resolve build cause and rerun scoped build, then install after successful build. |
| `dkms install` fails | All diagnostic state retained; DKMS may already have changed module/depmod/backup state | Inspect effective module, DKMS status and original-module backups before any recovery. Do not assume repeating install restores the previous module. |
| SIGTERM/INT/HUP before publication | Owned private staging cleaned; exit 128 + signal | Fix interruption cause and retry. |
| SIGTERM/INT/HUP during DKMS | Original process group signalled while its unreaped leader pins identity; bounded direct-child wait before lock release; partial state retained | Inspect for remaining/detached tasks as well as DKMS state before recovery; no full descendant-containment or rollback guarantee. |
| SIGKILL, power loss, cleanup failure or replaced staging pathname | Stale private staging may remain; published/DKMS state may also exist | Do not reuse or automatically purge stale paths. Inspect ownership, inode, contents, live processes and DKMS state while serialized. |

On a post-publication failure the helper prints fully scoped recovery commands
with the selected header path. These are alternatives depending on the first
incomplete step, **not** a script to run blindly. First inspect:

```sh
sudo dkms status -m hailo_pci -v 4.24.0
sudo cat /usr/src/hailo_pci-4.24.0/.hailo-install.log
```

DKMS build logs, when available, normally live under
`/var/lib/dkms/hailo_pci/VERSION/build/make.log` or
`VERSION/KERNEL/ARCH/log/make.log`. Missing/unreadable logs do not mask the DKMS
exit status: its command output is retained in the independent transcript.
Transcripts are local diagnostic data; inspect permissions before sharing them.

The installer deliberately refuses to overwrite a retained published tree on a
second invocation. Recovery uses the administrator-reviewed DKMS step, rather
than treating source/registration existence as permission for cleanup. If a
published tree has no registration, an administrator may instead archive that
exact verified tree after establishing no registration/build/backup references
need it; no automatic removal command or force option is provided. Stale staging
is never automatically scavenged. Do not remove original-module backups, other
versions, or dependency indexes as generic rollback.

DKMS hooks must run synchronously. Normal DKMS execution has no build timeout.
On either direct-child completion or interruption, the helper signals the
original process group before reaping its leader: `waitid(WNOWAIT)` keeps the
PID/PGID anchored, avoiding signals to a reused unrelated group. A default
SIGCHLD disposition is required; custom/automatic reaping is rejected before
mutation, and a non-waitable leader is never used for numeric group signals. Interruption
allows 0.25 seconds after SIGTERM, followed by SIGKILL; normal completion sends
SIGTERM/SIGKILL without a grace delay to stop lingering background hooks. The
subsequent direct-child wait is bounded to two seconds. Cleanup timeout is an
error with retained state, not permission to retry blindly.

This is bounded group signalling, **not full descendant containment** or proof
of descendant quiescence before unlocking. Descendants can detach with
`setsid()`/`setpgid()`, and tasks in uninterruptible sleep may remain after a
signal. The lock is held through group signalling and the bounded direct-child
wait, but is released on timeout too. The helper does not enumerate/reap all
orphaned descendants, signal reaped numeric PIDs, create a cgroup, or keep a
background lock holder. Administrators must ensure hooks are synchronous and
inspect remaining tasks before external lifecycle/recovery actions. Stronger
containment would require a separately managed supervisor/cgroup boundary.

Hashing uses bounded SHA-256 reads, not `hashlib.file_digest()`. No-follow,
nonblocking opens still prevent FIFO-swap hangs; only verified regular files
are hashed, and a would-block result fails instead of producing a partial or
incorrect digest. Cleanup uses descriptor-safe `shutil.rmtree()` through the
pinned `/proc/self/fd` parent path, avoiding its Python 3.11-only `dir_fd` keyword.
Python 3.10 syntax/API compatibility is tested here, but an actual Python 3.10
interpreter is not available in the validated Debian 13 image; runtime validation
uses Python 3.13.5.

**Only source publication is atomic.** DKMS registration/build/install, hooks,
module copying, signing and depmod are not an installer transaction. Even a
failed `add` may have changed state. No rollback, previous-module restoration,
crash durability (`fsync`), Secure Boot acceptance or protection against
uncooperative privileged writers is promised. Real failure/restoration tests
require a disposable VM. No module loading occurs in this helper.

### Signature and completion checks

DKMS owns signing through its framework configuration. A successful installer
exit does not prove a trusted, valid module signature or Secure Boot acceptance.
For example, DKMS 3.2.2 can warn after a sign-file failure and continue building.
Check the selected artifact and its certificate/trust independently; do not
disable Secure Boot or replace signing keys to bypass a failed check.

A final staging-cleanup error may occur after DKMS installation has completed.
Inspect the retained transcript, effective module and DKMS status before retry;
no rollback or automatic replacement of the retained source tree is attempted.

### Hardware-free regression tests

Inside the development container, run:

```sh
python3 -B -m unittest discover -s tests/dkms_lifecycle -v
python3 -B -m unittest discover -s tests/dkms_installation -v
```

These tests execute the Make targets, shell helpers and installation helper against private fixtures
below `.workbench/tmp/`. DKMS, deletion, depmod and the root prerequisite are
mocked; the processes remain non-root. No system module or DKMS state is used.
To additionally verify preservation of real compressed module artifacts, set
`HAILO_REAL_TEST_MODULE` to an absolute path to a previously GCC-built `.ko`
below `.workbench/tmp/`. The test copies/compresses that artifact and verifies
that the standalone refusal leaves every copy unchanged, including matching
name/version/kernel metadata; installation/removal commands remain fixture-only.

## Load the driver
Run the command `sudo modprobe hailo_pci` or `sudo modprobe -r hailo_pci`

## Set driver parameters
There are two options:
- Copy the `hailo_pci.conf` file to `/etc/modprobe.d` and edit the desired parameters.
- Pass the desired parameter in modprobe, for example:
  `sudo modprobe hailo_pci no_power_mode=Y`
