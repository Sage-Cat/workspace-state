# Parallel shutdown profiles

Shutdown profiles may opt into bounded parallel preparation with
`parallel = true`. Command profiles default to serialized execution; the
`qemu-windows-hibernate` adapter defaults to parallel execution. The default
value is omitted from rollback and startup-restore records so records written
by older versions retain their exact fingerprints.

Applicable profiles are probed by the coordinator. Consecutive parallel
profiles are grouped into batches of at most four workers, and the complete
batch is written to the rollback journal before any prepare operation starts.
A serialized profile forms a barrier before and after a batch. All workers are
joined before `run()` returns or raises; rollback therefore remains reverse
ordered and single-threaded.

Duplicate QEMU VM directories are rejected before any job prepares. Resource
locks also prevent overlapping operations on the same VM. Cancellation and critical failures stop cancellable peers, while
finish-then-rollback profiles are allowed to finish their in-flight prepare.
