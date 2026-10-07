import { useCallback, useEffect, useState } from "react";
import type { FormEvent, ReactNode } from "react";
import { api, ApiError } from "./api";
import { ErrorNotice } from "./components";
import ChatPage from "./ChatPage";
import AgentsPage from "./AgentsPage";
import ConfigPage from "./ConfigPage";
import type { JsonObject, PlatformState } from "./types";

type Page = "ai" | "agents" | "config";
type Theme = "system" | "light" | "dark";

function initialPage(): Page {
  const value = location.hash.slice(1);
  return value === "agents" || value === "config" ? value : "ai";
}

export function Login({
  onLogin,
  initialError = null,
}: {
  onLogin: () => void;
  initialError?: string | null;
}): ReactNode {
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(initialError);
  const [busy, setBusy] = useState(false);
  async function submit(event: FormEvent): Promise<void> {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const session = await api.login(password);
      if (session.authenticated) onLogin();
      else setError("登录未成功，请重试。");
    } catch (error) {
      setError(error instanceof Error ? error.message : String(error));
    } finally {
      setBusy(false);
    }
  }
  return (
    <main className="login-page">
      <div className="login-brand">
        <span className="brand-word">
          chiliarch<span>.</span>
        </span>
        <p>agent 调度工作区</p>
      </div>
      <form className="login-form" onSubmit={(event) => void submit(event)}>
        <p className="eyebrow">单用户登录</p>
        <h1>进入工作区</h1>
        <label htmlFor="login-password">登录密码</label>
        <input
          id="login-password"
          type="password"
          autoComplete="current-password"
          value={password}
          onChange={(event) => setPassword(event.target.value)}
          required
          autoFocus
        />
        <ErrorNotice error={error} />
        <button className="primary" disabled={busy || !password}>
          {busy ? "正在登录…" : "登录 →"}
        </button>
        <p className="muted small">使用初始化 chiliarch 时设置的密码。</p>
      </form>
      <footer>chiliarch · agent 调度平台</footer>
    </main>
  );
}

export default function App(): ReactNode {
  const [authenticated, setAuthenticated] = useState<boolean | null>(null);
  const [state, setState] = useState<PlatformState | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [page, setPage] = useState<Page>(initialPage);
  const [theme, setTheme] = useState<Theme>(() => {
    const stored = localStorage.getItem("chiliarch-theme");
    return stored === "dark" || stored === "light" ? stored : "system";
  });
  const [refreshing, setRefreshing] = useState(false);

  useEffect(() => {
    let active = true;
    void api
      .session()
      .then((session) => {
        if (active) setAuthenticated(session.authenticated);
      })
      .catch((error: unknown) => {
        if (active) {
          setAuthenticated(false);
          setError(error instanceof Error ? error.message : String(error));
        }
      });
    return () => {
      active = false;
    };
  }, []);
  useEffect(() => {
    const media = matchMedia("(prefers-color-scheme: dark)");
    const apply = (): void => {
      document.documentElement.dataset.theme =
        theme === "system" ? (media.matches ? "dark" : "light") : theme;
    };
    apply();
    localStorage.setItem("chiliarch-theme", theme);
    media.addEventListener("change", apply);
    return () => media.removeEventListener("change", apply);
  }, [theme]);
  useEffect(() => {
    const changed = (): void => setPage(initialPage());
    window.addEventListener("hashchange", changed);
    return () => window.removeEventListener("hashchange", changed);
  }, []);

  const loadState = useCallback(async (): Promise<void> => {
    setRefreshing(true);
    try {
      setState(await api.state());
      setError(null);
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) {
        setAuthenticated(false);
        setState(null);
      }
      setError(error instanceof Error ? error.message : String(error));
    } finally {
      setRefreshing(false);
    }
  }, []);
  const refresh = useCallback((): void => {
    void loadState();
  }, [loadState]);
  useEffect(() => {
    if (authenticated) void loadState();
  }, [authenticated, loadState]);
  const server = state?.config.server as JsonObject | undefined;
  const refreshSeconds =
    typeof server?.config_poll_seconds === "number"
      ? server.config_poll_seconds
      : 5;
  useEffect(() => {
    if (!authenticated) return;
    const timer = setInterval(refresh, refreshSeconds * 1000);
    return () => clearInterval(timer);
  }, [authenticated, refresh, refreshSeconds]);

  function navigate(next: Page): void {
    setPage(next);
    location.hash = next;
  }
  async function logout(): Promise<void> {
    try {
      await api.logout();
      setAuthenticated(false);
      setState(null);
      setError(null);
    } catch (error) {
      setError(error instanceof Error ? error.message : String(error));
    }
  }
  if (authenticated === null)
    return (
      <main className="boot-state">
        <span className="brand-word">
          chiliarch<span>.</span>
        </span>
        <p role="status">读取登录状态…</p>
      </main>
    );
  if (!authenticated)
    return (
      <Login
        initialError={error}
        onLogin={() => {
          setError(null);
          setAuthenticated(true);
        }}
      />
    );
  return (
    <div className="app-shell">
      <a className="skip-link" href="#workspace">
        跳转到工作区
      </a>
      <aside className="app-sidebar">
        <button
          className="brand plain"
          onClick={() => navigate("ai")}
          aria-label="chiliarch 首页"
        >
          <span className="brand-word">
            chiliarch<span>.</span>
          </span>
          <span className="brand-caption">agent workspace</span>
        </button>
        <nav aria-label="主导航">
          <button
            className={page === "ai" ? "active" : ""}
            aria-current={page === "ai" ? "page" : undefined}
            onClick={() => navigate("ai")}
          >
            <NavIcon type="ai" />
            总管对话
          </button>
          <button
            className={page === "agents" ? "active" : ""}
            aria-current={page === "agents" ? "page" : undefined}
            onClick={() => navigate("agents")}
          >
            <NavIcon type="agents" />
            agents
          </button>
          <button
            className={page === "config" ? "active" : ""}
            aria-current={page === "config" ? "page" : undefined}
            onClick={() => navigate("config")}
          >
            <NavIcon type="config" />
            配置
          </button>
        </nav>
        <div className="sidebar-bottom">
          <div className="platform-indicator">
            <span
              className={`connection-dot ${state && !error ? "online" : ""}`}
            />
            {state && !error ? "平台已连接" : error ? "连接异常" : "正在连接"}
          </div>
          <label className="theme-select">
            外观
            <select
              aria-label="外观"
              value={theme}
              onChange={(event) => setTheme(event.target.value as Theme)}
            >
              <option value="system">跟随系统</option>
              <option value="light">浅色</option>
              <option value="dark">深色</option>
            </select>
          </label>
          <button className="logout plain" onClick={() => void logout()}>
            退出登录 ↗
          </button>
        </div>
      </aside>
      <main id="workspace" className="workspace" tabIndex={-1}>
        {error && (
          <div className="global-error">
            <ErrorNotice error={error} />
            <button onClick={refresh} disabled={refreshing}>
              重新连接
            </button>
          </div>
        )}
        {state ? (
          page === "ai" ? (
            <ChatPage
              state={state}
              refresh={refresh}
              onConfigure={() => navigate("config")}
            />
          ) : page === "agents" ? (
            <AgentsPage state={state} refresh={refresh} />
          ) : (
            <ConfigPage state={state} refresh={refresh} />
          )
        ) : (
          <div className="boot-state">
            <p role="status">读取平台状态…</p>
          </div>
        )}
      </main>
    </div>
  );
}

function NavIcon({ type }: { type: Page }): ReactNode {
  return (
    <svg
      width="19"
      height="19"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      {type === "ai" ? (
        <>
          <path d="M5 4h14v12H9l-4 4V4Z" />
          <path d="M9 8h6M9 12h4" />
        </>
      ) : type === "agents" ? (
        <>
          <rect x="7" y="3" width="10" height="6" rx="1" />
          <rect x="2" y="15" width="7" height="6" rx="1" />
          <rect x="15" y="15" width="7" height="6" rx="1" />
          <path d="M12 9v3M5.5 15v-3h13v3" />
        </>
      ) : (
        <>
          <circle cx="12" cy="12" r="3" />
          <path
            d="m9 3 6 0 .6 3 2.4 1.4 2.8-.9 3 5.2-2.2 2.1v2.8l2.2 2.1-3 5.2-2.8-.9-2.4 1.4-.6 3H9l-.6-3L6 22l-2.8.9-3-5.2 2.2-2.1v-2.8L.2 10.7l3-5.2 2.8.9L8.4 5 9 3Z"
            transform="translate(3 0) scale(.75)"
          />
        </>
      )}
    </svg>
  );
}
