# Phase 7 controlled engineering investigation (checkpoint, not accepted)

The private service owns `ai_ops_tool_operations` inside the existing job store.
Every operation has a validated name and arguments, a job, exact source commit,
immutable input hash, worker lease, bounded result, status, and artifact hash.
Expired in-flight commands are marked interrupted and cannot be rerun under the
same operation ID. Completed records can be read again after a restart. Only
one engineering sandbox may run at a time.

The gateway constructs a source snapshot with `git archive` from an exact commit;
it rejects archive symlinks and unsafe paths. Candidate tests and patches are
written only into disposable snapshots, with a bounded provenance chain. The
snapshot is removed after the operation result is persisted. Candidate status
does not change a finding to RESOLVED. The gateway has no push, merge, deploy,
Railway mutation, Telegram, secret-read, network-client, package-install, or
arbitrary command endpoint.

The approved test runner constructs the entire `pytest` argument vector and
launches it through a network-isolated `bubblewrap` namespace with a minimal
environment, read-only source mount, timeout, output limit, and resource limits.
There is no fallback when this isolation cannot be created. Candidate source
text remains bounded and is screened before storage. DR stores sanitized
operation provenance and scanned candidate artifacts; raw command logs are not
exported.

**Acceptance blocker:** the present development host denies the network
namespace (`bwrap: loopback: Failed to create NETLINK_ROUTE socket: Operation
not permitted`). The deployed AI Ops image lacks bubblewrap, an approved
Python test runtime, and a pinned Git source repository. A real isolated
replay/test canary has not passed. These changes are a development checkpoint;
Phase 7 is not complete and must not be deployed as a tool execution service
until a supported isolated runner passes the security and end-to-end gates.
