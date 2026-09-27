# Phase 2 backup and recovery contract

Phase 2 passed its live-volume gate on 2026-09-27. The isolated Railway service
continues to run with the audit scheduler and real AI providers disabled.

## GitHub disaster recovery

- Target: a dedicated **private** `xtenrore/Plane-Alerts-Private-Repo` repository,
  separate from the public application source. Verify its visibility before
  writing any state.
- Export only the allowlisted job, step, checkpoint, status and hash fields.
  `app/private_ops/dr_export.py` never exports step output text. It rejects
  credential-like identifiers and exports over 2 MB. The SHA-256 manifest binds
  the exact source commit and counts. Completed steps preserve their output hashes;
  interrupted running work is restored as retryable work without worker leases.
- A durable, debounced outbound queue with a single claim lease,
  coalescing frequent changes, bounded retries and a verified remote checkpoint.
  A failed backup must not block local job processing. The remote writer must
  verify a read-back hash before acknowledging a checkpoint. Conflicts and
  network outages leave the pending checkpoint retryable. Immutable snapshots
  live under `private-ai-ops/snapshots/<sha256>.json` with an adjacent manifest.
- GitHub Actions credentials belong in GitHub Secrets. The default source repo
  `GITHUB_TOKEN` cannot be assumed to write to a different private repository.
  Configure a narrowly scoped backup writer credential through the existing
  GitHub-controlled deployment path only after the private destination exists.
  Never log, export or duplicate its value.
- Restore on a clean host into an empty dedicated directory, check the manifest
  and SQLite integrity, and verify completed work is skipped and interrupted
  work is resumable. A restore must not overwrite the live Railway volume.

On 2026-09-27, a **synthetic recreation** of the Phase 1 probe state was
uploaded to the private repository, read back and restored in a clean temporary
directory. Its manifest hash is
`b41232f2c654fddd45757a9a565cbbbbdef2b9ebaa55b639021a772108d05dae`.
This tests the remote namespace and restore mechanics; it is **not** a backup
of the actual Railway volume. The subsequent unattended worker used the
GitHub Actions Secret `AI_OPS_GITHUB_DR_TOKEN` and uploaded the actual Railway
volume state, manifest hash
`5577a4be3d6dae2d7e786e3cb5988fe50b2ddeac08a437716541d1d6e1b22400`.
The file was fetched from the private repository and restored into an empty
directory without access to the live volume: schema 2, two jobs, two completed
steps, verified Phase 1 probe checkpoint and SQLite integrity passed.

A one-time isolated fault probe rejected the first GitHub write, preserved
the existing completed job, retried successfully, and generated a verified
snapshot. The final fetched snapshot hash is
`5d423c27139c71055f8d75982b64994338b6aac8e0fc4c311c7dcb67b5cacdfd`.
Its manifest checksum, privacy scan and clean restore passed, including the
durable `phase2_fault_probe=verified` checkpoint. Repeated deployment with
unchanged state produced no duplicate snapshot. The test-only fault flag was
disabled through GitHub Actions and the final isolated deployment passed.

## Dropbox secondary backup (Phase 11 implementation)

- Dedicated root: `/Plane Alerts/Private AI Ops Backups/` in the owner's
  Dropbox. Do not share this root. The backend needs a dedicated app with
  app-folder access and offline OAuth refresh, minimum metadata/read/write
  scopes, and GitHub Actions Secrets equivalent to `DROPBOX_APP_KEY`,
  `DROPBOX_APP_SECRET` and `DROPBOX_REFRESH_TOKEN` when Phase 11 begins.
- Full snapshots require authenticated encryption with an independent
  owner-held recovery key. The key must also exist outside Railway/GitHub.
  Verify the remote content hash, enforce retention after testing restore, and
  track Dropbox status separately from GitHub status.
- The connected ChatGPT Dropbox connector is an owner inspection aid only.
  On 2026-09-27 its root listed zero entries and a `Plane Alerts` search found
  zero results. That confirms connector access, not unattended backend access
  or existence of a backup.

Independent status fields should include last attempted, last verified,
checkpoint hash, outcome (`pending`, `backed_up`, `delayed`, `failed`) and
retry count for each destination. Neither destination is the live database.
