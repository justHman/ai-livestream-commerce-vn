"""Backend outbound transport clients.

Hosted-provider clients live here (Task 1.22/1.32), never in the self-host
services. They own only request serialization, server-side credentials,
network I/O, bounded timeout/retry, response parsing, and typed transport
errors — no model-engine code, no API/Director imports.
"""
