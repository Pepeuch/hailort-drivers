# Hailo-8 upstream contribution status

Updated 2026-11-04. Targets: the upstream `hailo8` branches and the 4.24.0 host stack. This inventory records public topics, their dependencies and the validation actually available. An open PR is awaiting maintainer review or merge; a fork branch is an available proposal.

## Submitted topics

| Topic | Proposal | State | Software validation and scope |
|---|---|---|---|
| Runtime child shutdown error propagation | [HailoRT #49](https://github.com/hailo-ai/hailort/pull/49) | Open, target hailo8 | Child-status propagation tests and full library build; broader lifecycle/reentry guarantees remain outside the change. |
| Runtime one-shot shutdown result | [HailoRT #50](https://github.com/hailo-ai/hailort/pull/50) | Open, target hailo8 | Source-subset status/concurrency regressions and full build. A behavioral fixture for the complete resource-owning CoreOp still requires a supported driver backend; no bypass of its construction invariants is claimed. |
| Reset failure propagation | [Drivers #67](https://github.com/hailo-ai/hailort-drivers/pull/67) | Open, target hailo8 | 9/9 source-subset error-return cases; no real reset-failure injection claimed. |
| Reset timeout units | [Drivers #68](https://github.com/hailo-ai/hailort-drivers/pull/68) | Open, target hailo8 | Baseline 0/10, corrected 10/10 under ASan/UBSan; HZ/timeout arithmetic simulated. No actual kernel deadline measurement claimed. |
| Coherent DKMS installation/removal | [Drivers #69](https://github.com/hailo-ai/hailort-drivers/pull/69) | Open, target hailo8 | Installation/removal changes stay together. Latest removal rerun: 20 passed, one optional compiled-artifact case skipped. Privileged commands and DKMS backend are simulated. |
| Recoverable DKMS staging | [Fork stacked PR #1](https://github.com/Pepeuch/hailort-drivers/pull/1) | Open against prerequisite branch; depends on #69 | Latest rerun: 62 installation/review methods passed; 20 removal passed, one optional artifact case skipped. Filesystem/rename/flock checks are real; no system DKMS installation performed. |

## Prepared and dependent topics

| Topic | Dedicated fork branch | Readiness / dependencies |
|---|---|---|
| Completion armed before soft-reset request | [fix/pcie-reset-completion-order](https://github.com/Pepeuch/hailort-drivers/tree/fix/pcie-reset-completion-order) | Autonomous candidate directly on upstream hailo8; PR content prepared for owner review, no PR opened. Requires no timeout-unit, error-propagation or DKMS change. |
| Standalone DKMS source-layout adaptation | [adapt/dkms-standalone-layout](https://github.com/Pepeuch/hailort-drivers/tree/adapt/dkms-standalone-layout) | Dependent on DKMS staging and its prerequisite. Complements and credits [swancoder's upstream #56](https://github.com/hailo-ai/hailort-drivers/pull/56); do not duplicate that proposal. Seven historical added layout cases pass; submission base must follow the prerequisite's reviewed integration. |

The completion-order candidate is at `e72fb432ab32d10d3edcaef8fad89cb5ec04d2f9`. Its production change moves completion reinitialization before sending the request, so an acknowledgment delivered during that send is retained. A standalone fixture compiles the exact wait/reset/boot-IRQ bodies: **3/7 before, 7/7 after**, under ASan/UBSan. A real module builds against Debian AMD64 6.12.113 headers without warnings. Conversion/tick fields are observations only; the independent timeout-unit topic remains separate.

These completion-order tests simulate kernel/MMIO dependencies. No real device open, ioctl, module loading, reset, firmware fault injection or hardware test is part of this validation. Late old interrupts, reset generation attribution, global reset serialization and physical DMA retirement are outside this correction.

## Stable and experimental compositions

| Composition | Head / purpose |
|---|---|
| [Drivers fix/all](https://github.com/Pepeuch/hailort-drivers/tree/fix/all) | `072c1082e0597137a8600a1bf2ac60c96ce3606d`; existing stable software integration reference. Completion-order correction is not merged here. |
| [HailoRT fix/all](https://github.com/Pepeuch/hailort/tree/fix/all) | `5f99c6fd165cb5fcaa82aebae44e13025a0454e5`; existing stable software integration reference, including the credited [build contribution #34](https://github.com/hailo-ai/hailort/pull/34). |
| [Experimental completion ordering on Drivers fix/all](https://github.com/Pepeuch/hailort-drivers/tree/experimental/r01-on-fix-all) | `4caadf9c1703c40b6ce9fa083ac1947c6469719c`; isolated composition with the autonomous topic. Automatic merge succeeded; 7/7 ordering cases pass. Production bytes match the already compiled development composition. No change to stable fix/all. |
| [Earlier composed ordering WIP](https://github.com/Pepeuch/hailort-drivers/tree/wip/r01-completion-order) | `99c57148a3ca914757721d3add5c777bacf61435`; retained development checkpoint based on fix/all. Use the autonomous candidate above as the proposed upstream head. |

## Contribution workflow

1. Bound a demonstrated defect and create a dedicated topic on the correct Hailo-8 base.
2. Run relevant software regressions/builds and retain the baseline failures and limitations.
3. Review the full reachable history, metadata and changed files before publishing the topic. Simulate the push and verify its remote head after publication.
4. Present the proposed PR contents and validation evidence to the owner before opening an additional upstream PR.
5. Update this inventory when a meaningful topic, validation or review state changes. Keep dependent changes together or explicitly stacked; avoid duplicate or artificially split proposals.
6. Qualify a separate experimental composition before proposing a stable fix/all update. Maintainer acceptance, upstream merge and fork integration are recorded as separate events.

The next independent proposed submission is the completion-order correction. The next dependent submission boundary is DKMS staging after its coherent removal prerequisite; its standalone-layout follow-up requires alignment with that staging base and attribution to #56. No additional independent correction is announced as validated here.
