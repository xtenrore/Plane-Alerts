# Phase 2 backup and recovery contract

Phase 2 is **in progress**. The isolated Railway service continues to run with
the scheduler and real providers disabled. A sanitized portable export exists,
but no remote backup or clean-host remote restore has passed its gate yet.

## GitHub disaster recovery

- Target: a dedicated **private** `xtenrore/Plane-Alerts-Private-State` repository,
  separate from the public application source. Verify its visibility before
  writing any state.
- Export only the allowlisted job, step, checkpoint, status and hash fields.
  `app/private_ops/dr_export.py` rejects unknown private strings, coordinates,
  credential-like text and exports over 2 MB. The SHA-256 manifest binds the
  exact source commit and counts. Completed steps preserve their output hashes;
  interrupted running work is restored as retryable work without worker leases.
- Implement a durable, debounced outbound queue with a single claim lease,
  coalescing frequent changes, bounded retries and a verified remote checkpoint.
  A failed backup must not block local job processing. The remote writer must
  verify a read-back hash before acknowledging a checkpoint. Conflicts and
  network outages leave the pending checkpoint retryable.
- GitHub Actions credentials belong in GitHub Secrets. The default source repo
  `GITHUB_TOKEN` cannot be assumed to write to a different private repository.
  Configure a narrowly scoped backup writer credential through the existing
  GitHub-controlled deployment path only after the private destination exists.
  Never log, export or duplicate its value.
- Restore on a clean host into an empty dedicated directory, check the manifest
  and SQLite integrity, and verify completed work is skipped and interrupted
  work is resumable. A restore must not overwrite the live Railway volume.

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
