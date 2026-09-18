# Attachment adapter

This is a host-to-script protocol, not a file upload UI. The skill cannot change where Claude/Codex first stores an attachment. Only use a path actually supplied by the host for a user-uploaded attachment, or materialize the exact original bytes with a supported host attachment-download tool. Do not search the computer for a similarly named file, assume a pasted path is an uploaded attachment, transcribe model-visible text, or claim an attachment was accessible when only extracted text was supplied.

Create a UTF-8 JSON manifest in the configured bot home's `requests` directory (create it when needed). Paths are internal arguments; the user never types them. Use the original filename, not the host's temporary filename.

```json
{
  "source": "chat_attachment",
  "host": "codex",
  "order_known": true,
  "attachments": [
    {"attachment_id": "actual-host-attachment-id-or-locator", "name": "制度.pdf", "path": "C:/actual/host/provided/file.pdf"}
  ]
}
```

`host` is `codex` or `claude`. `attachment_id` is the host ID or exact host-provided attachment locator, not a fabricated proof of provenance. The backend validates readable bytes, path shape, supported format, hash and database state; it cannot cryptographically attest that a local file was uploaded. The skill is responsible for host provenance.

`add --manifest manifest.json` copies originals into `<registered workspace>/bot documents/<doc-id>/<original name>` before returning the queue receipt, so temporary attachments may safely expire. It does not run the worker itself; start the queue pump immediately after showing receipts.

Use all attachments explicitly selected in the current add request. If the user says add after an earlier upload, use that upload only while it is still available and unambiguous. Never silently ingest unrelated older attachments. If attachment order is unknown, set `order_known:false` for filename ordering.

For same-name different-content conflicts, ask keep or replace. Save the resulting choice separately, keyed by attachment ID:

```json
{"attachment-id": {"action": "keep"}}
```

or

```json
{"attachment-id": {"action": "replace", "document_id": "exact-old-document-id"}}
```

Then resubmit `add --manifest ... --choices ...`. Already queued identical contents return duplicate receipts. Remove temporary manifest/choices files after resolved ingestion; do not remove host-managed originals. Unsupported attachments need to be reattached using a compatible local host version, not converted to manual path inputs.
