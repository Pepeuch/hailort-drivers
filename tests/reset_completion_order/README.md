# Soft-reset completion ordering regression

Run as an ordinary user with Python 3, GCC, ASan and UBSan:

```sh
python3 -B tests/reset_completion_order/reproduce.py --source-root . --output /new/results
```

The output directory must not exist. To compare an older local Git revision,
add `--revision REVISION`; the checkout is never changed. The suite reuses
the extractor from `tests/reset_timeout_units` and compiles the unedited
production wait helper, soft-reset method and boot IRQ handler in a C fixture.

An acknowledgment is delivered synchronously from the simulated request write
to exercise the ordering boundary deterministically. Other cases cover an
acknowledgment during the wait, no acknowledgment, boot-poll failure, a stale
completion counter and two consecutive acknowledged requests. Kernel APIs,
MMIO, firmware and PCIe are simulated. This is not a kernel or hardware test.

The change only avoids clearing a new response after sending its request.
It does not distinguish a late old interrupt from a new response, drain pending
interrupts, serialize reset callers, prove DMA retirement or implement recovery.
The fixture does not claim coverage of those unresolved contracts.
