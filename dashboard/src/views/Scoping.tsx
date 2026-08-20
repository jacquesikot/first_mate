import { useEffect, useRef, useState } from "react";
import type { ScopingChat } from "../api";
import { api, socket } from "../api";
import { useApp } from "../components";
import { Markdown } from "../markdown";
import { splitPath } from "../format";

/** The scoping conversation, rendered inside the task session it belongs to.
 * Turns run on the daemon off the request path, so this polls/listens rather
 * than holding a request open (PRD §6.8, open question §10.1). */
export function ScopingPanel({
  chat: initial,
  onApproved,
}: {
  chat: ScopingChat;
  onApproved: () => void;
}) {
  const { toast, go, refresh } = useApp();
  const [chat, setChat] = useState<ScopingChat>(initial);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const bottom = useRef<HTMLDivElement>(null);
  const chatId = initial.id;

  // The daemon owns the turn; follow it. WS pushes a `scoping` frame when a
  // turn lands, and the poll is the backstop while one is in flight.
  const thinking = chat.status === "thinking";
  useEffect(() => {
    let alive = true;
    const pull = () =>
      api
        .scopeGet(chatId)
        .then((d) => {
          if (alive) setChat(d.chat);
        })
        .catch(() => {});
    const unsub = socket.subscribe((msg) => {
      if ("kind" in msg && (msg as { kind: string }).kind === "scoping") pull();
    });
    const timer = thinking ? window.setInterval(pull, 1500) : null;
    return () => {
      alive = false;
      unsub();
      if (timer != null) window.clearInterval(timer);
    };
  }, [chatId, thinking]);

  useEffect(() => {
    bottom.current?.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }, [chat.messages.length, thinking]);

  const send = async (text: string) => {
    if (!text.trim() || busy || thinking) return;
    setBusy(true);
    setDraft("");
    try {
      const d = await api.scopeMessage(chat.id, text.trim());
      setChat(d.chat);
    } catch (e) {
      toast("Turn failed", String(e), "bad");
      api.scopeGet(chatId).then((d) => setChat(d.chat)).catch(() => {});
    } finally {
      setBusy(false);
    }
  };

  const approve = async (run: boolean) => {
    if (busy) return;
    setBusy(true);
    try {
      const d = await api.scopeApprove(chat.id, run);
      toast(
        "Contract approved",
        run && d.started ? "worker spawning" : `start it with fm run ${d.task.id}`,
        "run"
      );
      refresh();
      onApproved();
    } catch (e) {
      toast("Approve failed", String(e), "bad");
    } finally {
      setBusy(false);
    }
  };

  const abandon = async () => {
    try {
      await api.scopeAbandon(chat.id);
      refresh();
      go("#/tasks");
    } catch (e) {
      toast("Abandon failed", String(e), "bad");
    }
  };

  const contract = chat.contract as Record<string, any> | null;
  const failed = chat.status === "failed";

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 18, minWidth: 0 }}>
      <div
        className="mono"
        style={{
          display: "flex",
          alignItems: "center",
          gap: 12,
          padding: "10px 14px",
          borderRadius: "var(--rad)",
          border: "1px solid var(--bd)",
          background: "var(--s2)",
          fontSize: 11,
          flexWrap: "wrap",
          minWidth: 0,
        }}
      >
        <span
          className={thinking ? "pulse" : ""}
          style={{ width: 6, height: 6, borderRadius: "50%", background: "var(--ac)" }}
        />
        <span style={{ color: "var(--tx2)" }}>scoping · {splitPath(chat.repo).name}</span>
        <span style={{ color: "var(--tx4)" }}>{chat.status.replace(/_/g, " ")}</span>
        <span style={{ marginLeft: "auto", display: "flex", gap: 12, flex: "0 0 auto" }}>
          <span style={{ color: "var(--tx4)" }}>{chat.id}</span>
          {chat.status !== "approved" && (
            <button className="mono" style={{ fontSize: 10.5, color: "var(--tx3)" }} onClick={abandon}>
              abandon
            </button>
          )}
        </span>
      </div>

      <div style={{ display: "flex", flexDirection: "column", gap: 20, minWidth: 0 }}>
        {chat.messages.map((m, i) => (
          <Turn key={i} role={m.role} text={m.text} />
        ))}
        {thinking && (
          <ThinkingRow first={chat.messages.length === 0} goal={chat.goal} repo={chat.repo} />
        )}
        {chat.messages.length === 0 && !thinking && (
          <div className="empty">no turns yet</div>
        )}
      </div>

      {contract && (
        <ContractCard contract={contract} errors={chat.contract_errors} />
      )}

      {chat.status === "contract_ready" && (
        <div style={{ display: "flex", alignItems: "center", gap: 9, flexWrap: "wrap" }}>
          <button
            className="btn accent"
            style={{ padding: "9px 16px", fontSize: 13 }}
            disabled={busy}
            onClick={() => approve(true)}
          >
            Approve and run
          </button>
          <button className="btn" disabled={busy} onClick={() => approve(false)}>
            Approve only
          </button>
          <span className="mono" style={{ fontSize: 10.5, color: "var(--tx4)" }}>
            then nothing downstream is interactive by default
          </span>
        </div>
      )}

      {chat.status !== "approved" && chat.status !== "abandoned" && (
        <div
          style={{
            display: "flex",
            gap: 10,
            alignItems: "center",
            padding: "12px 14px",
            borderRadius: "var(--rad)",
            border: `1px solid ${failed ? "var(--badbd)" : "var(--bd2)"}`,
            background: "var(--s2)",
            minWidth: 0,
          }}
        >
          <input
            className="line"
            placeholder={
              thinking
                ? "waiting for the assistant…"
                : failed
                  ? "the last turn failed — send again to resume the conversation…"
                  : "push back, add a constraint, or say it looks right…"
            }
            value={draft}
            disabled={busy || thinking}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") send(draft);
            }}
          />
          <button
            className="mono"
            style={{ fontSize: 11.5, color: "var(--ac)", flex: "0 0 auto" }}
            disabled={busy || thinking}
            onClick={() => send(draft)}
          >
            send ↵
          </button>
        </div>
      )}
      <div ref={bottom} />
    </div>
  );
}

function Turn({ role, text }: { role: string; text: string }) {
  const label = role === "firstmate" ? "first mate" : role === "system" ? "note" : "you";
  const color =
    role === "firstmate" ? "var(--ac)" : role === "system" ? "var(--bad)" : "var(--tx3)";
  return (
    <div className="turn rise">
      <div className="turn-who">
        <span className="label" style={{ color }}>
          {label}
        </span>
      </div>
      {role === "firstmate" ? (
        <Markdown text={text} className="turn-body" />
      ) : (
        <div
          className="turn-body"
          style={{
            whiteSpace: "pre-wrap",
            overflowWrap: "anywhere",
            color: role === "system" ? "var(--bad)" : "var(--tx)",
          }}
        >
          {text}
        </div>
      )}
    </div>
  );
}

function ThinkingRow({
  first,
  goal,
  repo,
}: {
  first: boolean;
  goal: string;
  repo: string;
}) {
  return (
    <div className="turn">
      <div className="turn-who" />
      <div className="turn-body" style={{ minWidth: 0 }}>
        <div
          className="rise"
          style={{
            display: "flex",
            alignItems: "center",
            gap: 12,
            padding: "13px 15px",
            borderRadius: "var(--rad)",
            border: "1px solid var(--acbd)",
            background: "var(--acbg)",
            minWidth: 0,
          }}
        >
          <span
            className="pulse"
            style={{ width: 7, height: 7, borderRadius: "50%", background: "var(--ac)", flex: "0 0 7px" }}
          />
          <div style={{ display: "flex", flexDirection: "column", gap: 3, minWidth: 0 }}>
            <span style={{ fontSize: 13.5 }}>
              {first ? "Reading the repo and project memory…" : "Thinking…"}
            </span>
            <span className="mono truncate" style={{ fontSize: 10.5, color: "var(--tx3)" }}>
              {goal} · {splitPath(repo).name}
            </span>
          </div>
        </div>
      </div>
    </div>
  );
}

function ContractCard({
  contract,
  errors,
}: {
  contract: Record<string, any>;
  errors: string[];
}) {
  const criteria: any[] = contract.criteria ?? [];
  return (
    <div className="card-flat" style={{ minWidth: 0 }}>
      <div
        style={{
          padding: "9px 14px",
          borderBottom: "1px solid var(--bd)",
          display: "flex",
          alignItems: "center",
          gap: 9,
          flexWrap: "wrap",
        }}
      >
        <span className="label" style={{ color: "var(--tx3)" }}>
          contract
        </span>
        <span
          className="mono"
          style={{
            marginLeft: "auto",
            fontSize: 10.5,
            color: errors.length ? "var(--bad)" : "var(--ok)",
          }}
        >
          {errors.length
            ? `${errors.length} error${errors.length > 1 ? "s" : ""}`
            : `${criteria.length} criteria · all machine-checkable`}
        </span>
      </div>
      <div style={{ padding: "12px 14px", display: "flex", flexDirection: "column", gap: 8 }}>
        <Row k="goal" v={String(contract.goal ?? "")} />
        <Row k="scope" v={(contract.scope_in ?? []).join(" · ")} />
        <Row k="steps" v={(contract.steps ?? []).map((s: any) => s.id).join(" → ")} />
        {criteria.map((c) => (
          <Row key={c.id} k={c.id} v={c.command} accent />
        ))}
        {errors.map((e, i) => (
          <Row key={`e${i}`} k="✗" v={e} bad />
        ))}
      </div>
    </div>
  );
}

function Row({
  k,
  v,
  accent,
  bad,
}: {
  k: string;
  v: string;
  accent?: boolean;
  bad?: boolean;
}) {
  return (
    <div style={{ display: "flex", gap: 14, minWidth: 0 }}>
      <span
        className="mono"
        style={{ fontSize: 11, color: "var(--tx4)", flex: "0 0 86px", overflowWrap: "anywhere" }}
      >
        {k}
      </span>
      <span
        className="mono"
        style={{
          fontSize: 11.5,
          lineHeight: 1.55,
          flex: 1,
          minWidth: 0,
          color: bad ? "var(--bad)" : accent ? "var(--tx2)" : "var(--tx)",
          overflowWrap: "anywhere",
        }}
      >
        {v}
      </span>
    </div>
  );
}
