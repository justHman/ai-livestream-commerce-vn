# P0-FB-007 autonomous opening

The existing `p0.execution.v1` command endpoint now implements `start`.
Hold/Resume and other P0 rescue commands retain their unsupported results.
API owns merchant authorization; Runtime verifies the exact tenant, business
session, Runtime session and execution generation before activation.

1. Bind and attach the approved ScriptSet using the existing 005 path.
   Attach is dormant; even an ingested comment cannot activate an approved
   session in place of the start command.
2. The trusted media adapter/test fixture submits `runtime_ready` to the
   existing admin-authenticated execution evidence endpoint. `media_readiness`
   contains `readiness_id`, `destination_id`, `source_id`, `media_ready` and
   `platform_ready`. These are scoped, non-secret references. Runtime resolves
   the approved envelope and records its fingerprint with readiness.
3. The API owner/admin command facade accepts `command: start` and the exact
   execution identity the caller reviewed. It checks its durable mapping,
   Runtime capabilities, dependency health, approved fingerprint and media
   readiness. It derives a stable command ID and timestamp rather than trusting
   client retry IDs. Runtime rechecks current health, content and attachment.
4. Runtime saves the accepted intent under the existing session lock/fence,
   then queues one opening in the existing Coordinator. The first product's
   complete locked approved artifact supplies the opening, with no generated
   greeting or unapproved template. The existing Director continues bounded
   selling and approved-product progression after that one opening.
5. `applied / opening_scheduled` means activation was applied, not that media
   played. `execution.opening_media` correlates the opening turn with the actual
   guarded audio utterance, or with provider completion on the cloud path.
   This local receipt alone does not mark first playable.
6. Only a trusted `first_ai_broadcast` evidence event matching the execution,
   selected media references, opening turn and emitted-media receipt can set
   `first_ai_broadcast` and `first_playable_evidence_id`. Duplicate sequence or
   repeated first-live assertions are rejected without changing that state.

The command ID is `start:` followed by SHA-256 of the UTF-8 encoding of the four
identity values in tenant/business/Runtime/generation order. Each value is
prefixed with its UTF-8 byte length and a colon. Its opening turn is the command
ID plus `:opening`. API uses the durable Runtime mapping's creation timestamp.
Semantic retries return the original actor/result; ambiguous accepted intent
never authorizes another opening. Provider timeouts during this opening are
not blindly retried. A lost process after accepted intent may require later
reconciliation; this task provides no crash replay or recovery owner.

The existing stop path cancels generation/preparation immediately, then shares
the execution lock for teardown so a late command receipt cannot recreate
deleted session metadata. No new End/Interrupt/Hold behavior is introduced.

## Controlled cross-repository verification

With the backend test environment, from `services/product/backend_service`:

```text
python tests/fixtures/autonomous_start_server.py 19977
```

In the sibling API checkout, set `P0_FB_007_RUNTIME_URL=http://127.0.0.1:19977`,
then run:

```text
go test ./cmd/ai-connector-service/internal/usecase -run TestAutonomousStartRealRuntimeIntegration -count=1 -v
```

The fixture binds only to loopback, uses real app/HTTP/Coordinator/speech code
with controlled authoring/media providers, and intentionally loses one start
response. Run it with a fresh fixture process. Stop that process after testing.
The media assertion is explicitly fixture evidence. Actual Facebook publication
and external viewer receipt remain task 010; credit accounting remains task 018.
