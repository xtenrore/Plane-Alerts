# Phase 6 review checkpoint

Private AI Operations runs only in the isolated owner service. The hourly AI
scheduler and all production flight-state mutation paths remain disabled.

The first reviewer receives a bounded, sanitized deterministic evidence packet.
Cases that require review retain the validated first result before a second call.
The second reviewer receives the same original evidence without the first
classification or explanation. A second key for the same provider/model is not
an independent opinion: the case remains pending until a different approved
provider and model are available. The recorded review relationship names the
provider, model, slot name, blind status and independence quality.

Agreement on a low-impact classification requires no third call. A disagreement
or high-impact agreement queues a bounded deep review. Deep review requires a
separately configured, allowlisted free model under the `deep:<provider>` route
key; without it the work remains durable and pending. A deep review never
replaces the canonical finding classification on model agreement alone. The
case retains a deterministic investigation requirement for later replay, tests
and physical outcome verification. Phase 6 does not give models repository or
shell tools.

Schema 6 adds durable escalation reasons, independence labels and one feedback
verdict per finding. The sanitized DR schema 4 exports and restores these fields
without credentials, prompts, raw provider bodies or exact private coordinates.
Provider usage and feedback summaries are observational; router selection never
reads quality scores. An account-scoped or unspecified quota response cools the
whole provider pool. Only an explicitly key-scoped response permits trying a
peer key. Cloudflare remains reserved for Supervisor by default.

The exact live-provider inference canary is a separate gate. Metadata access
does not establish a free billing tier or prove inference. Do not claim the
canary complete until the approved free account/model route is verified and a
bounded shadow packet has completed both provider families, persisted across
restart, and passed the production isolation check.
