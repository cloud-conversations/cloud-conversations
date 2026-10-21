import { Link } from "react-router-dom";
import { ApiError, LOGOUT_URL } from "../api/client";
import { useMe } from "../hooks/useMe";
import { MAX, MIN, useSidebarWidth } from "../hooks/useSidebarWidth";
import { AnnouncementBanner } from "./AnnouncementBanner";
import { Failed } from "./Failed";
import { ThreadSidebar } from "./ThreadSidebar";
import { WaitingBanner } from "./WaitingBanner";

/** The cloud in a speech bubble. */
function Logo() {
  return (
    <svg viewBox="0 0 32 32" className="w-8 h-8 flex-shrink-0" aria-hidden="true">
      <circle cx="16" cy="16" r="15" fill="#fff" stroke="currentColor" strokeWidth="2" />
      <path
        fill="currentColor"
        d="M11 20a3.2 3.2 0 0 1-.3-6.4 4.6 4.6 0 0 1 8.7-1.3 3.6 3.6 0 0 1 .9 7.7H11Z"
      />
      <path fill="currentColor" d="M13 19.6h4l-3.4 4.6-.6-4.6Z" />
    </svg>
  );
}

const BOX = "px-3 py-1 rounded-md border border-grey-light bg-white";
const LINK = "text-link hover:bg-brand-info/10 hover:border-brand-info/40";

export function Layout({ children }: { children: React.ReactNode }) {
  const sidebar = useSidebarWidth();
  const me = useMe();

  if (!me.data) {
    const loggedOut = me.error instanceof ApiError && me.error.status === 401;
    if (me.error && !loggedOut) {
      return (
        <div className="p-4">
          <Failed error={me.error} />
        </div>
      );
    }
    return null;
  }

  return (
    <div className="h-screen flex flex-col">
      <header className="bg-brand-info/10 border-b border-brand-info/20 shadow-xs">
        <div className="px-4 h-16 flex items-center gap-3">
          <span className="text-cc-blue">
            <Logo />
          </span>
          <div className="flex flex-col justify-center leading-tight">
            <Link
              to="/"
              className="self-start text-2xl font-bold text-cc-navy tracking-tight hover:text-link"
            >
              Cloud Conversations
            </Link>
            <p className="text-xs text-grey">
              Testbed: <span className="font-medium text-grey-dark">{me.data.testbed}</span>
            </p>
          </div>
          <div className="ml-auto flex items-center gap-2 text-sm">
            <span className={`${BOX} text-grey-dark`}>{me.data.username}</span>
            <Link to="/profile" className={`${BOX} ${LINK}`}>
              Profile
            </Link>
            {me.data.admin && (
              <Link to="/admin" className={`${BOX} ${LINK}`}>
                Admin
              </Link>
            )}
            <form method="post" action={LOGOUT_URL}>
              <button type="submit" className={`${BOX} ${LINK}`}>
                Log out
              </button>
            </form>
          </div>
        </div>
      </header>
      <AnnouncementBanner />
      <WaitingBanner />
      <div className="flex-1 flex min-h-0">
        <ThreadSidebar width={sidebar.width} />
        <div
          role="separator"
          aria-orientation="vertical"
          aria-label="Experiment list width"
          aria-valuenow={sidebar.width}
          aria-valuemin={MIN}
          aria-valuemax={MAX}
          tabIndex={0}
          onMouseDown={sidebar.start}
          onDoubleClick={sidebar.reset}
          onKeyDown={(e) => {
            if (e.key === "ArrowLeft") sidebar.nudge(-24);
            if (e.key === "ArrowRight") sidebar.nudge(24);
          }}
          title="Drag to resize, double-click to reset"
          className="w-1.5 flex-shrink-0 cursor-col-resize bg-grey-light hover:bg-brand-info focus:bg-brand-info focus:outline-hidden"
        />
        <main className="flex-1 min-w-0 flex flex-col">{children}</main>
      </div>
    </div>
  );
}
