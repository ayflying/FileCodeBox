# P2P Direct Transfer

P2P direct transfer is the flagship feature this fork adds on top of upstream: files **never touch the server**. The publisher's and downloader's browsers connect directly over WebRTC—zero-wait publishing, zero server footprint.

## How it works

In the traditional flow, a file must be fully uploaded to the server before a passcode can be shared. P2P direct transfer changes that:

```
Traditional:  Publisher ──upload──► Server storage ──download──► Downloader
P2P direct:   Publisher ◄────WebRTC direct────► Downloader
                     (server handles signaling only)
```

- **Signaling** (exchanging connection info) goes through the server—negligible traffic;
- **File data** travels peer-to-peer between the two browsers, encrypted end-to-end (DTLS) with integrity verification;
- The server **never stores or relays** file content.

## Publishing: check “P2P direct transfer”

1. On the home page, choose “File sharing” and add a file;
2. Keep the “P2P direct transfer” toggle checked (**enabled by default**);
3. Click upload—the file is not transmitted, and the **passcode appears instantly**;
4. Share the passcode with the recipient.

After publishing, the page shows online status and a completed-transfer counter. **Keep the page open** until the recipient finishes retrieving—closing the publisher page invalidates the share immediately, and downloaders see a clear message.

::: info Expiry for P2P shares
P2P shares **do not support “expire by retrieval count”** (data cannot be recalled once delivered). Only day / hour / minute / forever expiry modes are available. The effective share lifetime is the earlier of the publisher page lifetime and the configured expiry.
:::

## Retrieving: direct download

1. Enter the passcode on the home page;
2. The page detects a P2P share and shows the direct-transfer panel—click “Direct retrieval”;
3. The browser establishes a direct connection and transfer begins, with live progress;
4. On completion the file is integrity-checked, then saved.

**Saving depends on the site protocol**:

| Site | Save method | Size limit |
|------|-------------|------------|
| **HTTPS** | Streams straight to disk (File System Access API) | No software cap—only disk space |
| **HTTP** | Buffered in memory, then saved | Above 1 GB, automatic volume split (`.001` / `.002` …); merge with the command shown on the page |

::: tip Prefer HTTPS
For production, configure HTTPS: the downloader streams files directly to disk with no volume splitting, giving the most complete experience.
:::

## Size and quota

- **No per-file size cap** (`p2pMaxSize=0`): P2P files never touch server disk; size is bounded only by the two devices;
- **No storage quota usage**: P2P shares don't count toward `storageLimit` or consume download counts;
- **No empty admin records**: P2P shares never appear in the admin file management list.

## Connectivity (NAT traversal)

WebRTC direct connections require NAT traversal, and the server ships with a built-in STUN service:

- **Self-hosted STUN**: the container listens on `3478/udp`—keep this port open when deploying;
- **Public fallback**: additional public STUN servers are configured, so hole-punching still works if the self-hosted port is unreachable;
- **CGNAT filtering**: candidates in CGNAT virtual-network ranges such as `100.64.0.0/10` are automatically filtered, preventing traffic from being detoured through VPN/tunnel interfaces and throttled.

Direct connections succeed in most home-broadband and mobile scenarios. **If both ends sit behind strict NATs** (corporate networks, some symmetric NATs), the direct connection fails with a clear message—fall back to regular upload (server-relayed) in that case.

::: warning About the server relay fallback
The design includes a “streaming relay” fallback mode (server relays without storing when direct connection fails), but **its data plane is not yet implemented**. In the current version, a failed direct connection reports an error explicitly rather than silently degrading.
:::

## Admin configuration

Administrators can tune the following P2P settings (all ship with sensible defaults):

| Setting | Default | Description |
|---------|---------|-------------|
| `enableP2P` | `1` | Master switch; hides P2P features when off |
| `p2pDefaultChecked` | `1` | Whether the P2P toggle is pre-checked on the upload page |
| `p2pMaxSize` | `0` | Per-file size cap in bytes; `0` = unlimited |
| `p2pMaxPeers` | `3` | Max concurrent downloaders per share |
| `p2pHeartbeatTimeout` | `30` | Publisher heartbeat timeout in seconds before going offline |
| `p2pStunEnabled` | `1` | Whether the built-in STUN service is enabled |
| `p2pStunPort` | `3478` | Built-in STUN listen port (UDP) |
| `p2pStunUrls` | empty | Custom STUN list; empty = auto-derived from the site's access entry |
| `p2pRelayEnabled` | `1` | Relay mode switch (data plane not yet implemented; reserved) |

## FAQ

**Can the recipient still retrieve after the publisher closes the page?**
No. The P2P data source is the publisher's browser—closing the page invalidates the share. Use regular upload when you need “publish and walk away”.

**Download stuck on connecting / direct connection failed?**
NAT traversal failed. Verify the server's `3478/udp` is reachable; if it still fails, use regular upload.

**Transfer speed is underwhelming?**
Check that neither device runs virtual-networking software (tools that assign `100.64.0.0/10` CGNAT addresses)—this version filters such addresses automatically, but carrier QoS throttling can still apply in extreme cases. Direct speed is bounded by the two ends' upload bandwidth.

**Download asks to save volume files?**
The site is HTTP (insecure context), so the browser disallows streaming to disk. Merge the volumes per the on-page instructions—or configure HTTPS once and be done.

## Design details

For the full technical design (state machines, signaling protocol, security design, staged implementation log), see [p2p-design.md](https://github.com/ayflying/FileCodeBox/blob/master/docs/p2p-design.md).
