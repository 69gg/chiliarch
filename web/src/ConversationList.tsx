import { useState } from "react";
import type { ReactNode } from "react";
import { ErrorNotice, time } from "./components";
import type { Conversation } from "./types";

type ConversationEdit = {
  conversation: Conversation;
  kind: "rename" | "delete";
  title: string;
};

export function ConversationList({
  conversations,
  selected,
  busy,
  active,
  onChoose,
  onCreate,
  onRename,
  onDelete,
  onClose,
  onPin,
  pinned,
}: {
  conversations: Conversation[];
  selected: string | null;
  busy: boolean;
  active: boolean;
  onChoose: (id: string) => void;
  onCreate: () => void;
  onRename: (id: string, title: string) => Promise<void>;
  onDelete: (id: string) => Promise<void>;
  onClose: () => void;
  onPin: () => void;
  pinned: boolean;
}): ReactNode {
  const [edit, setEdit] = useState<ConversationEdit | null>(null);
  const [error, setError] = useState<string | null>(null);

  function begin(conversation: Conversation, kind: "rename" | "delete"): void {
    setEdit({ conversation, kind, title: conversation.title });
    setError(null);
    onPin();
  }
  function cancelEdit(): void {
    setEdit(null);
    setError(null);
  }
  async function save(): Promise<void> {
    if (!edit || busy || (edit.kind === "rename" && !edit.title.trim())) return;
    setError(null);
    try {
      if (edit.kind === "rename")
        await onRename(edit.conversation.id, edit.title.trim());
      else await onDelete(edit.conversation.id);
      setEdit(null);
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : String(failure));
    }
  }

  return (
    <div className="conversation-list">
      <div className="conversation-list-heading">
        <h2>最近会话</h2>
        <div className="conversation-heading-actions">
          {!pinned && (
            <button
              className="plain conversation-pin"
              type="button"
              aria-label="固定会话列表"
              title="固定会话列表"
              onClick={onPin}
            >
              <svg
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                aria-hidden="true"
              >
                <path d="m15 3 6 6-3 1-4 4v4l-3-3-6 6 6-6-3-3h4l4-4z" />
              </svg>
            </button>
          )}
          <button
            className="plain"
            type="button"
            aria-label="关闭会话列表"
            data-drawer-close
            onClick={onClose}
          >
            ×
          </button>
        </div>
      </div>
      <button className="new-conversation" disabled={busy} onClick={onCreate}>
        ＋ 新会话
      </button>
      {conversations.length === 0 && <p className="muted small">尚无会话</p>}
      {conversations.map((conversation) => {
        const name = conversation.title || "新会话";
        const editing = edit?.conversation.id === conversation.id;
        const cannotDelete = busy || (active && selected === conversation.id);
        return (
          <div
            key={conversation.id}
            className={`conversation-row${selected === conversation.id ? " selected" : ""}`}
          >
            <button
              disabled={busy}
              className="conversation-entry"
              onClick={() => {
                cancelEdit();
                onChoose(conversation.id);
              }}
              aria-pressed={selected === conversation.id}
              title={name}
            >
              <span>{name}</span>
              <small>
                {conversation.source === "agent"
                  ? "来自 agent"
                  : time(conversation.updated_at)}
              </small>
            </button>
            <div className="conversation-row-actions">
              <button
                className="plain"
                type="button"
                aria-label={`重命名 ${name}`}
                title="重命名"
                disabled={busy}
                onClick={() => begin(conversation, "rename")}
              >
                <svg
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  aria-hidden="true"
                >
                  <path d="m16 4 4 4M4 20l4-1L20 7a2.8 2.8 0 0 0-4-4L4 15z" />
                </svg>
              </button>
              <button
                className="plain"
                type="button"
                aria-label={`删除 ${name}`}
                title={
                  cannotDelete && !busy ? "先停止运行，再删除会话" : "删除会话"
                }
                disabled={cannotDelete}
                onClick={() => begin(conversation, "delete")}
              >
                <svg
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  aria-hidden="true"
                >
                  <path d="M4 6h16M9 6V3h6v3M6 6l1 15h10l1-15M10 10v7m4-7v7" />
                </svg>
              </button>
            </div>
            {editing && (
              <form
                className="conversation-edit"
                aria-label={
                  edit.kind === "rename" ? `重命名 ${name}` : `确认删除 ${name}`
                }
                onSubmit={(event) => {
                  event.preventDefault();
                  void save();
                }}
              >
                {edit.kind === "rename" ? (
                  <label>
                    会话名称
                    <input
                      value={edit.title}
                      maxLength={256}
                      required
                      disabled={busy}
                      autoFocus
                      onChange={(event) =>
                        setEdit({ ...edit, title: event.target.value })
                      }
                    />
                  </label>
                ) : (
                  <p>删除“{name}”及其历史记录？此操作无法撤销。</p>
                )}
                <ErrorNotice error={error} />
                <div className="conversation-edit-actions">
                  <button type="button" disabled={busy} onClick={cancelEdit}>
                    取消
                  </button>
                  <button
                    className={edit.kind === "delete" ? "danger" : "primary"}
                    disabled={
                      busy || (edit.kind === "rename" && !edit.title.trim())
                    }
                  >
                    {edit.kind === "rename" ? "保存名称" : "确认删除"}
                  </button>
                </div>
              </form>
            )}
          </div>
        );
      })}
    </div>
  );
}
