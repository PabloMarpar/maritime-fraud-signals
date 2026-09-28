// Live AIS relay for the public site: AISStream -> browsers, as a Cloudflare Worker.
//
// AISStream forbids direct browser connections ("connect from your own server and proxy only the
// information each client needs") and allows 3 connections per account, so every viewer shares
// ONE upstream connection held by a single Durable Object ("main"). It is the same relay as
// ingest/aisstream.py (same browser protocol), with two differences that keep it inside the
// Workers Free plan:
//   - the upstream connection opens with the first viewer and closes IDLE_CLOSE_MS after the last
//     one leaves, so nothing runs while nobody is watching;
//   - only pages from ALLOWED_ORIGINS may connect, so other sites cannot spend the quota.
// Nothing is stored. The key is a Worker secret (`wrangler secret put AISSTREAM_API_KEY`), never
// in the repo; locally it comes from relay/.dev.vars (git-ignored).
//
// Browser protocol (unchanged): on connect {"type":"snapshot","vessels":[...],"stats":{...}};
// then every second {"type":"update","vessels":[changed],"removed":[mmsi],"stats":{...}}.

import { DurableObject } from 'cloudflare:workers';
import { REGIONS, applyMessage, compact, prune, subscription, type Vessel } from './ais.ts';

export interface Env {
  RELAY: DurableObjectNamespace<LiveRelay>;
  AISSTREAM_API_KEY: string;
  /** Comma-separated page origins allowed to connect; empty allows any (local development). */
  ALLOWED_ORIGINS?: string;
}

const AISSTREAM_URL = 'wss://stream.aisstream.io/v0/stream';
const BROADCAST_EVERY_MS = 1000;
const IDLE_CLOSE_MS = 5 * 60_000;
const RECONNECT_MS = 5000;

export default {
  async fetch(request, env): Promise<Response> {
    const url = new URL(request.url);
    const relay = env.RELAY.get(env.RELAY.idFromName('main'));
    if (url.pathname === '/health') return relay.fetch(request);
    if (request.headers.get('Upgrade')?.toLowerCase() !== 'websocket') {
      return new Response('Live AIS relay for the maritime-fraud-signals site. Connect with a WebSocket.\n', {
        status: 426,
      });
    }
    const allowed = (env.ALLOWED_ORIGINS ?? '').split(',').map((s) => s.trim()).filter(Boolean);
    const origin = request.headers.get('Origin') ?? '';
    if (allowed.length && !allowed.includes(origin)) return new Response('Origin not allowed\n', { status: 403 });
    return relay.fetch(request);
  },
} satisfies ExportedHandler<Env>;

export class LiveRelay extends DurableObject<Env> {
  vessels = new Map<number, Vessel>();
  dirty = new Set<number>();
  clients = new Set<WebSocket>();
  upstream: WebSocket | null = null;
  status = 'idle';
  error: string | null = null;
  received = 0;
  rateWindow: [number, number][] = [];
  ticker: ReturnType<typeof setInterval> | null = null;
  idleSince: number | null = null;
  reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  decoder = new TextDecoder();

  async fetch(request: Request): Promise<Response> {
    if (new URL(request.url).pathname === '/health') {
      return Response.json({ ...this.stats(), viewers: this.clients.size });
    }
    const pair = new WebSocketPair();
    const [client, server] = Object.values(pair);
    server.accept();
    this.clients.add(server);
    this.idleSince = null;
    const drop = () => {
      this.clients.delete(server);
      try {
        server.close(1000, 'bye'); // complete the closing handshake the viewer started
      } catch {
        // already closed
      }
    };
    server.addEventListener('close', drop);
    server.addEventListener('error', drop);
    this.ensureUpstream();
    this.ensureTicker();
    prune(this.vessels, Date.now() / 1000);
    server.send(JSON.stringify({ type: 'snapshot', vessels: this.positioned(), stats: this.stats() }));
    return new Response(null, { status: 101, webSocket: client });
  }

  stats() {
    const now = Date.now() / 1000;
    this.rateWindow = this.rateWindow.filter(([t]) => now - t <= 10);
    this.rateWindow.push([now, this.received]);
    const [oldestT, oldestN] = this.rateWindow[0];
    const rate = now > oldestT ? (this.received - oldestN) / (now - oldestT) : 0;
    let positioned = 0;
    for (const v of this.vessels.values()) if (v.la !== undefined) positioned++;
    return {
      upstream: this.status,
      error: this.error,
      vessels: positioned,
      msgs_per_s: Math.round(rate * 10) / 10,
      regions: Object.keys(REGIONS),
      server_time: Math.round(now * 10) / 10,
    };
  }

  positioned(): Vessel[] {
    const out: Vessel[] = [];
    for (const v of this.vessels.values()) if (v.la !== undefined) out.push(compact(v));
    return out;
  }

  ensureUpstream() {
    if (this.upstream || this.reconnectTimer) return;
    if (!this.env.AISSTREAM_API_KEY) {
      this.status = 'error';
      this.error = 'AISSTREAM_API_KEY is not set on the relay';
      return;
    }
    this.status = this.status === 'idle' ? 'connecting' : 'reconnecting';
    let ws: WebSocket;
    try {
      // The constructor negotiates permessage-deflate, which AISStream requires for full rate.
      ws = new WebSocket(AISSTREAM_URL);
      ws.binaryType = 'arraybuffer';
    } catch (e) {
      this.error = String(e).slice(0, 200);
      this.scheduleReconnect();
      return;
    }
    this.upstream = ws;
    ws.addEventListener('open', () => {
      // AISStream closes the connection unless the subscription arrives within 3 s.
      ws.send(JSON.stringify(subscription(this.env.AISSTREAM_API_KEY, Object.keys(REGIONS))));
      this.status = 'live';
      this.error = null;
    });
    ws.addEventListener('message', async (event) => {
      // AISStream sends binary frames; depending on the runtime they arrive as Blob or ArrayBuffer.
      const data: unknown = event.data;
      let msg: any;
      try {
        const text =
          typeof data === 'string'
            ? data
            : data instanceof Blob
              ? await data.text()
              : this.decoder.decode(data as ArrayBuffer);
        msg = JSON.parse(text);
      } catch {
        return;
      }
      if (msg && typeof msg === 'object' && 'error' in msg) {
        // e.g. an invalid key; AISStream closes the connection right after.
        this.error = String(msg.error).slice(0, 200);
        return;
      }
      this.received++;
      const mmsi = applyMessage(this.vessels, msg, Date.now() / 1000);
      if (mmsi !== null) this.dirty.add(mmsi);
    });
    const lost = () => {
      if (this.upstream !== ws) return;
      this.upstream = null;
      if (this.clients.size) this.scheduleReconnect();
      else this.status = 'idle';
    };
    ws.addEventListener('close', lost);
    ws.addEventListener('error', lost);
  }

  scheduleReconnect() {
    this.status = 'reconnecting';
    if (this.reconnectTimer) return;
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      if (this.clients.size) this.ensureUpstream();
    }, RECONNECT_MS);
  }

  ensureTicker() {
    if (this.ticker) return;
    this.ticker = setInterval(() => this.tick(), BROADCAST_EVERY_MS);
  }

  tick() {
    const now = Date.now();
    const removed = prune(this.vessels, now / 1000);
    if (!this.clients.size) {
      this.dirty.clear();
      this.idleSince ??= now;
      if (now - this.idleSince > IDLE_CLOSE_MS) this.sleep();
      return;
    }
    const changed: Vessel[] = [];
    for (const m of this.dirty) {
      const v = this.vessels.get(m);
      if (v && v.la !== undefined) changed.push(compact(v));
    }
    this.dirty.clear();
    const frame = JSON.stringify({ type: 'update', vessels: changed, removed, stats: this.stats() });
    for (const ws of this.clients) {
      try {
        ws.send(frame);
      } catch {
        this.clients.delete(ws);
      }
    }
  }

  /** Nobody is watching: close the upstream and stop the clock so the object can be evicted. */
  sleep() {
    const ws = this.upstream;
    this.upstream = null;
    ws?.close(1000, 'no viewers');
    if (this.ticker) clearInterval(this.ticker);
    if (this.reconnectTimer) clearTimeout(this.reconnectTimer);
    this.ticker = null;
    this.reconnectTimer = null;
    this.idleSince = null;
    this.status = 'idle';
  }
}
