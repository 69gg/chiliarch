import { lazy, Suspense, useCallback, useEffect, useState } from "react";
import type { FormEvent, ReactNode } from "react";
import { api, ApiError } from "./api";
import { ErrorNotice } from "./components";
import ChatPage from "./ChatPage";
import AgentsPage from "./AgentsPage";
import ConfigPage from "./ConfigPage";
import GoalsPage from "./GoalsPage";
import CollaborationPage from "./CollaborationPage";
import NativeInteractions from "./NativeInteractions";
import type { JsonObject, PlatformState } from "./types";

const ProjectsPage = lazy(() => import("./ProjectsPage"));

type Page = "ai" | "agents" | "config" | "goals" | "collaboration" | "projects";
type Theme = "system" | "light" | "dark";
const themes: Theme[] = ["system", "light", "dark"];
const themeNames: Record<Theme, string> = {
  system: "跟随系统",
  light: "浅色",
  dark: "深色",
};

function initialPage(): Page {
  const value = location.hash.slice(1).split("/")[0];
  return value === "agents" ||
    value === "config" ||
    value === "goals" ||
    value === "collaboration" ||
    value === "projects"
    ? value
    : "ai";
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
          Chiliarch<span>.</span>
        </span>
      </div>
      <form className="login-form" onSubmit={(event) => void submit(event)}>
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
      </form>
      <footer>Chiliarch · agent 调度平台</footer>
    </main>
  );
}

export default function App(): ReactNode {
  const [authenticated, setAuthenticated] = useState<boolean | null>(null);
  const [state, setState] = useState<PlatformState | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [page, setPage] = useState<Page>(initialPage);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(
    () => localStorage.getItem("chiliarch-sidebar-collapsed") === "true",
  );
  const [theme, setTheme] = useState<Theme>(() => {
    const stored = localStorage.getItem("chiliarch-theme");
    return stored === "dark" || stored === "light" ? stored : "system";
  });
  const [systemDark, setSystemDark] = useState(
    () => matchMedia("(prefers-color-scheme: dark)").matches,
  );
  const effectiveTheme =
    theme === "system" ? (systemDark ? "dark" : "light") : theme;
  const nextTheme = themes[(themes.indexOf(theme) + 1) % themes.length];
  const themeTitle = `当前${themeNames[theme]}${theme === "system" ? `（${themeNames[effectiveTheme]}）` : ""}，切换为${themeNames[nextTheme]}`;
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
    const apply = (): void => setSystemDark(media.matches);
    apply();
    media.addEventListener("change", apply);
    return () => media.removeEventListener("change", apply);
  }, []);
  useEffect(() => {
    document.documentElement.dataset.theme = effectiveTheme;
    localStorage.setItem("chiliarch-theme", theme);
  }, [theme, effectiveTheme]);
  useEffect(() => {
    localStorage.setItem(
      "chiliarch-sidebar-collapsed",
      String(sidebarCollapsed),
    );
  }, [sidebarCollapsed]);
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
          Chiliarch<span>.</span>
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
    <div className={`app-shell${sidebarCollapsed ? " sidebar-collapsed" : ""}`}>
      <a className="skip-link" href="#workspace">
        跳转到工作区
      </a>
      <aside className="app-sidebar" aria-label="工作区导航">
        <div className="sidebar-header">
          <button
            className="brand plain"
            onClick={() => navigate("ai")}
            aria-label="Chiliarch 首页"
          >
            <span className="brand-word">
              Chiliarch<span>.</span>
            </span>
          </button>
          <button
            type="button"
            className="sidebar-toggle"
            aria-label={sidebarCollapsed ? "展开导航栏" : "收起导航栏"}
            title={sidebarCollapsed ? "展开导航栏" : "收起导航栏"}
            aria-expanded={!sidebarCollapsed}
            aria-controls="main-navigation"
            onClick={() => setSidebarCollapsed((collapsed) => !collapsed)}
          >
            <SidebarIcon collapsed={sidebarCollapsed} />
          </button>
        </div>
        <nav id="main-navigation" aria-label="主导航">
          <button
            className={page === "ai" ? "active" : ""}
            aria-label="总管对话"
            title={sidebarCollapsed ? "总管对话" : undefined}
            aria-current={page === "ai" ? "page" : undefined}
            onClick={() => navigate("ai")}
          >
            <NavIcon type="ai" />
            <span className="nav-label">总管对话</span>
          </button>
          <button
            className={page === "goals" ? "active" : ""}
            aria-label="目标"
            title={sidebarCollapsed ? "目标" : undefined}
            aria-current={page === "goals" ? "page" : undefined}
            onClick={() => navigate("goals")}
          >
            <NavIcon type="goals" />
            <span className="nav-label">目标</span>
          </button>
          <button
            className={page === "projects" ? "active" : ""}
            aria-label="项目"
            title={sidebarCollapsed ? "项目" : undefined}
            aria-current={page === "projects" ? "page" : undefined}
            onClick={() => navigate("projects")}
          >
            <NavIcon type="projects" />
            <span className="nav-label">项目</span>
          </button>
          <button
            className={page === "collaboration" ? "active" : ""}
            aria-label="协作"
            title={sidebarCollapsed ? "协作" : undefined}
            aria-current={page === "collaboration" ? "page" : undefined}
            onClick={() => navigate("collaboration")}
          >
            <NavIcon type="collaboration" />
            <span className="nav-label">协作</span>
          </button>
          <button
            className={page === "agents" ? "active" : ""}
            aria-label="Agents 管理"
            title={sidebarCollapsed ? "Agents 管理" : undefined}
            aria-current={page === "agents" ? "page" : undefined}
            onClick={() => navigate("agents")}
          >
            <NavIcon type="agents" />
            <span className="nav-label">Agents 管理</span>
          </button>
          <button
            className={page === "config" ? "active" : ""}
            aria-label="配置"
            title={sidebarCollapsed ? "配置" : undefined}
            aria-current={page === "config" ? "page" : undefined}
            onClick={() => navigate("config")}
          >
            <NavIcon type="config" />
            <span className="nav-label">配置</span>
          </button>
        </nav>
        <div className="sidebar-bottom">
          <button
            type="button"
            className="theme-toggle"
            aria-label={themeTitle}
            title={themeTitle}
            onClick={() => setTheme(nextTheme)}
          >
            <ThemeIcon theme={theme} />
          </button>
          <button
            className="logout plain"
            aria-label="退出登录"
            title={sidebarCollapsed ? "退出登录" : undefined}
            onClick={() => void logout()}
          >
            <span className="nav-label">退出登录</span>
            <svg
              width="17"
              height="17"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.5"
              strokeLinecap="round"
              strokeLinejoin="round"
              aria-hidden="true"
            >
              <path d="M9 4H4v16h5M14 8l4 4-4 4M8 12h10" />
            </svg>
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
        {state && <NativeInteractions state={state} refresh={refresh} />}
        {state ? (
          page === "ai" ? (
            <ChatPage
              state={state}
              refresh={refresh}
              onConfigure={() => navigate("config")}
            />
          ) : page === "goals" ? (
            <GoalsPage state={state} refresh={refresh} />
          ) : page === "projects" ? (
            <Suspense
              fallback={
                <p className="project-page-loading" role="status">
                  读取项目工作区…
                </p>
              }
            >
              <ProjectsPage state={state} refresh={refresh} />
            </Suspense>
          ) : page === "collaboration" ? (
            <CollaborationPage state={state} refresh={refresh} />
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

function SidebarIcon({ collapsed }: { collapsed: boolean }): ReactNode {
  return (
    <svg
      width="18"
      height="18"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <rect x="3" y="4" width="18" height="16" rx="2" />
      <path d="M9 4v16" />
      <path d={collapsed ? "m13 9 3 3-3 3" : "m16 9-3 3 3 3"} />
    </svg>
  );
}

function ThemeIcon({ theme }: { theme: Theme }): ReactNode {
  return (
    <svg
      width="18"
      height="18"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      {theme === "system" ? (
        <>
          <rect x="3" y="4" width="18" height="13" rx="2" />
          <path d="M8 21h8M12 17v4" />
        </>
      ) : theme === "light" ? (
        <>
          <circle cx="12" cy="12" r="4" />
          <path d="M12 2v2M12 20v2M2 12h2M20 12h2M5 5l1.5 1.5M17.5 17.5 19 19M5 19l1.5-1.5M17.5 6.5 19 5" />
        </>
      ) : (
        <path d="M20.8 14.2A9 9 0 0 1 9.8 3.2 9 9 0 1 0 20.8 14.2Z" />
      )}
    </svg>
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
      ) : type === "goals" ? (
        <>
          <circle cx="12" cy="12" r="8" />
          <circle cx="12" cy="12" r="3" />
          <path d="m12 12 8-8M16 4h4v4" />
        </>
      ) : type === "projects" ? (
        <>
          <path d="M3 7h7l2-3h9v16H3V7Z" />
          <path d="M7 11h10M7 15h6" />
        </>
      ) : type === "collaboration" ? (
        <>
          <path d="M3 4h12v9H8l-4 3v-3H3V4Z" />
          <path d="M18 8h3v11h-3v3l-4-3H9v-3" />
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
