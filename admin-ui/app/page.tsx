"use client";

/**
 * Token admin panel.
 *
 * All state is server-derived: the token list is re-fetched after every mutation
 * rather than patched locally. Revoking is security-relevant, so the screen must
 * reflect what the service actually did, not what the browser hoped it did.
 *
 * No credential ever lives in this component. Sign-in posts the admin key to
 * /api/session, which verifies it and stores it in an httpOnly cookie; every later
 * call goes through /api/tokens with no key in sight.
 */

import { useCallback, useEffect, useState } from "react";

type TokenInfo = {
  id: string;
  name: string;
  created_at: number;
  expires_at: number | null;
  last_used_at: number | null;
};

type TokenList = { tokens: TokenInfo[]; count: number; static_keys: number };
type Created = TokenInfo & { secret: string };

const DAY_OPTIONS = [
  { label: "Never expires", value: "" },
  { label: "30 days", value: "30" },
  { label: "90 days", value: "90" },
  { label: "180 days", value: "180" },
  { label: "365 days", value: "365" },
];

function when(ts: number | null): string {
  if (!ts) return "—";
  return new Date(ts * 1000).toLocaleString(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  });
}

function expiryLabel(token: TokenInfo): string {
  if (token.expires_at === null) return "Never";
  const daysLeft = Math.ceil((token.expires_at * 1000 - Date.now()) / 86_400_000);
  if (daysLeft <= 0) return "Expired";
  if (daysLeft <= 7) return `${daysLeft}d left`;
  return when(token.expires_at);
}

async function readError(response: Response): Promise<string> {
  try {
    const body = (await response.json()) as { error?: string };
    return body.error ?? `HTTP ${response.status}`;
  } catch {
    return `HTTP ${response.status}`;
  }
}

export default function Page() {
  const [signedIn, setSignedIn] = useState<boolean | null>(null);
  const [adminKey, setAdminKey] = useState("");
  const [list, setList] = useState<TokenList | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const [name, setName] = useState("");
  const [days, setDays] = useState("");
  const [created, setCreated] = useState<Created | null>(null);
  const [copied, setCopied] = useState(false);

  const refresh = useCallback(async () => {
    const response = await fetch("/api/tokens");
    if (response.status === 401) {
      setSignedIn(false);
      setList(null);
      return;
    }
    if (!response.ok) {
      setError(await readError(response));
      return;
    }
    setError(null);
    setList((await response.json()) as TokenList);
    setSignedIn(true);
  }, []);

  useEffect(() => {
    void (async () => {
      const response = await fetch("/api/session");
      const { signedIn: active } = (await response.json()) as { signedIn: boolean };
      if (active) await refresh();
      else setSignedIn(false);
    })();
  }, [refresh]);

  async function signIn(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const response = await fetch("/api/session", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ adminKey }),
      });
      if (!response.ok) {
        setError(await readError(response));
        return;
      }
      setAdminKey(""); // don't leave it in a form field
      await refresh();
    } finally {
      setBusy(false);
    }
  }

  async function signOut() {
    await fetch("/api/session", { method: "DELETE" });
    setSignedIn(false);
    setList(null);
    setCreated(null);
  }

  async function create(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    setCreated(null);
    setCopied(false);
    try {
      const response = await fetch("/api/tokens", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name, expiresInDays: days ? Number(days) : null }),
      });
      if (!response.ok) {
        setError(await readError(response));
        return;
      }
      setCreated((await response.json()) as Created);
      setName("");
      await refresh();
    } finally {
      setBusy(false);
    }
  }

  async function revoke(token: TokenInfo) {
    // Irreversible and immediate — anything using this token starts getting 401s.
    const ok = window.confirm(
      `Revoke "${token.name}"?\n\nAny app using it will start getting 401 responses straight away. This cannot be undone — you would have to issue a new token.`,
    );
    if (!ok) return;

    setBusy(true);
    setError(null);
    try {
      const response = await fetch(`/api/tokens/${encodeURIComponent(token.id)}`, {
        method: "DELETE",
      });
      if (!response.ok && response.status !== 204) {
        setError(await readError(response));
        return;
      }
      if (created?.id === token.id) setCreated(null);
      await refresh();
    } finally {
      setBusy(false);
    }
  }

  async function copySecret() {
    if (!created) return;
    try {
      await navigator.clipboard.writeText(created.secret);
      setCopied(true);
    } catch {
      setError("Could not copy — select the text and copy it manually.");
    }
  }

  if (signedIn === null) {
    return (
      <main>
        <p className="empty">Loading…</p>
      </main>
    );
  }

  if (!signedIn) {
    return (
      <main>
        <header>
          <div>
            <h1>Search Service — API Tokens</h1>
            <p className="sub">Sign in with an admin key to issue tokens.</p>
          </div>
        </header>

        <h2>Sign in</h2>
        {error && <div className="alert error">{error}</div>}
        <form className="card" onSubmit={signIn}>
          <label htmlFor="adminKey">Admin key</label>
          <input
            id="adminKey"
            type="password"
            autoComplete="off"
            value={adminKey}
            onChange={(e) => setAdminKey(e.target.value)}
            placeholder="a value from SERVICE_API_KEYS"
          />
          <p className="hint">
            This must be one of the service&apos;s <code>SERVICE_API_KEYS</code>, not a
            token issued here — issued tokens deliberately cannot mint more tokens. It is
            held in an httpOnly cookie for 8 hours and never stored by this app.
          </p>
          <div style={{ marginTop: 16 }}>
            <button type="submit" disabled={busy || !adminKey.trim()}>
              {busy ? "Checking…" : "Sign in"}
            </button>
          </div>
        </form>
      </main>
    );
  }

  return (
    <main>
      <header>
        <div>
          <h1>Search Service — API Tokens</h1>
          <p className="sub">
            {list ? `${list.count} issued` : "—"}
            {list && list.static_keys > 0 && (
              <>
                {" · "}
                {list.static_keys} static admin key
                {list.static_keys === 1 ? "" : "s"} in <code>.env</code>
              </>
            )}
          </p>
        </div>
        <button className="ghost" onClick={signOut}>
          Sign out
        </button>
      </header>

      {error && <div className="alert error">{error}</div>}

      {created && (
        <>
          <h2>New token</h2>
          <div className="reveal">
            <strong>Copy this now — it is shown once.</strong>
            <p style={{ margin: 0, fontSize: 13.5 }}>
              Only a hash is stored, so it cannot be shown again. If it is lost, revoke
              this token and issue another.
            </p>
            <div className="secret">
              <input readOnly value={created.secret} onFocus={(e) => e.target.select()} />
              <button onClick={copySecret}>{copied ? "Copied" : "Copy"}</button>
            </div>
            <p className="hint">
              <code>{created.name}</code> · id <code>{created.id}</code> · send it as the{" "}
              <code>X-API-Key</code> header
            </p>
          </div>
        </>
      )}

      <h2>Issue a token</h2>
      <form className="card" onSubmit={create}>
        <div className="row">
          <div>
            <label htmlFor="name">App name</label>
            <input
              id="name"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="blog-app"
              maxLength={120}
            />
          </div>
          <div style={{ flex: "0 0 170px" }}>
            <label htmlFor="days">Expiry</label>
            <select id="days" value={days} onChange={(e) => setDays(e.target.value)}>
              {DAY_OPTIONS.map((o) => (
                <option key={o.value} value={o.value}>
                  {o.label}
                </option>
              ))}
            </select>
          </div>
          <button type="submit" disabled={busy || !name.trim()}>
            {busy ? "Working…" : "Create"}
          </button>
        </div>
        <p className="hint">
          Give each app its own token. They get separate rate-limit budgets, so one app
          looping cannot starve another — and you can revoke one without touching the rest.
        </p>
      </form>

      <h2>Live tokens</h2>
      <div className="card scroll">
        {!list || list.tokens.length === 0 ? (
          <p className="empty">
            No tokens yet.
            {list && list.static_keys > 0
              ? " The static keys in .env still work."
              : ""}
          </p>
        ) : (
          <table>
            <thead>
              <tr>
                <th>Name</th>
                <th>ID</th>
                <th>Created</th>
                <th>Expires</th>
                <th>Last used</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {list.tokens.map((token) => (
                <tr key={token.id}>
                  <td>{token.name}</td>
                  <td className="mono">{token.id}</td>
                  <td>{when(token.created_at)}</td>
                  <td>
                    {token.expires_at === null ? (
                      <span className="pill">Never</span>
                    ) : (
                      expiryLabel(token)
                    )}
                  </td>
                  <td>{token.last_used_at ? when(token.last_used_at) : "Never used"}</td>
                  <td style={{ textAlign: "right" }}>
                    <button className="link" disabled={busy} onClick={() => revoke(token)}>
                      Revoke
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </main>
  );
}
