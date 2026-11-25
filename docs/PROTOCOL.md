# Redtooth transfer protocol v1

A native phone or desktop client interoperates by implementing this.

## Discovery

Each device registers with the directory (`PUT /v1/me/devices`) with
`{id, name, kind, has_app, wifi_direct, endpoints:[{kind:"lan"|"wifi_direct", host, port}]}`,
re-sent every ~20 s and whenever addresses change. Audio devices a user shares carry
`{id, name, kind:"headphones", address:"AA:BB:…", model}` and no endpoints.

Contact discovery: `POST /v1/lookup {hashes:[sha256("redtooth:" + E.164)]}`.

## Framing

TCP. Each frame is a 4-byte big-endian length, then that many bytes of UTF-8 JSON (the header). DATA headers carry `len`; that many raw bytes follow. Header ≤ 64 KiB, chunk ≤ 8 MiB (senders use 1 MiB).

## Probe

```
→ {"type":"PING"}
← {"type":"PONG","device_id":"…","name":"…"}
```
Senders must check `device_id`, so a stale address is never used.

## Transfer

```
→ OFFER   {id, name, size, mime, fingerprint, chunk, from:{name, phone}}
← WAITING {}                       recipient is being asked
← ACCEPT  {offset, hashes} | DECLINE {reason}
→ DATA    {offset, len, b2|s256} + bytes   chunk hash, hex: b2 = BLAKE2b-128, s256 = SHA-256
→ END     {sha256}                 full-file SHA-256
← SAVED   {name, sha256, accept_to_first_byte_ms, first_byte_to_saved_ms, resumed_from}
← ERROR   {reason, resume_offset}
↔ CANCEL  {}
```

- `hashes` lists the chunk hashes the receiver can verify, in preference order (desktop: `["b2","s256"]`, phones: `["s256"]`). The sender uses the first one it supports. A missing list means `["b2"]` (v1 receivers).
- `id` is stable across reconnects of one transfer. `fingerprint` = sha1(name:size:mtime_ns) identifies the file version.
- On a repeat OFFER with the same `id` and `fingerprint`, a receiver that already accepted replies ACCEPT with the last verified offset and does not ask again.
- The receiver writes to a hidden partial file, verifies every chunk before writing it, checkpoints the verified offset (fsync) every 8 MiB and on disconnect, re-hashes the verified prefix on resume, and checks the full SHA-256 before the atomic rename into the inbox.
- The sender reports *delivered* only after SAVED.
- If a closed connection ends before SAVED, the sender reconnects (exponential backoff, re-probing routes) and resumes.
