# MVP contract

Observe -> capture immutable evidence -> annotate -> preview -> select an existing
agent pane -> draft without submission. The agent conversation stays in that pane.

1. Kubernetes transport exposes only scoped GET reads. No mutation or exec action.
2. Each environment owns a persistent transport and Stern collector. Network and
   agent discovery/delivery work run outside the UI event loop. One failed scope
   cannot block healthy environments. Data age and connection failures are visible.
3. Selection is `(context, namespace, kind, UID)`, never a row index. Polling does not
   silently select another resource. If a resource disappears, show that explicitly.
4. Newest-first log records retain duplicates, full UTC timestamps, context,
   namespace, pod and container. History and collection are bounded. Display
   filtering never destroys collected records. Pause freezes the displayed history.
5. Capture freezes selected records and resource state. A note's content is never
   refreshed from live data. Excerpts are limited to 50 records and 32 KiB; omitted
   records/bytes are disclosed. Users can edit the excerpt and instruction.
6. Agent picker always asks, includes all local-session agents, prioritizes the
   current workspace, shows location/state, and remembers the last successful target.
   Picker rows freeze while open. No agents are spawned by the app.
7. Before drafting, revalidate pane and available session identity. Paste as literal
   bracketed text, strip terminal control sequences, never submit Enter. A target
   change refuses. A delivery timeout is uncertain, not automatically retryable.
   Failure preserves notes; confirmed delivery consumes only the sent batch. Focus
   failure after delivery is still delivered and cannot trigger duplication.
8. No evidence, raw logs or prompt text is written to application diagnostic logs.
   Configuration/cache live outside the package. No credentials are copied/exported.
9. Standalone install, offline demo, documented config, dependency diagnostics and
   optional herdr launcher. No host-specific paths, agents, clusters or services in code.
10. Test keyboard behavior, frozen capture, identity reconciliation, boundaries,
    high-volume buffers, collector cleanup, and all handoff failure outcomes. Target
    p95 key-to-render under 100ms; display logs within 1s of local receipt.

No agent result tracking, automatic remediation, tickets, web service, Windows or
remote-session routing in this MVP. Original dashboard/workspaces stay untouched.
