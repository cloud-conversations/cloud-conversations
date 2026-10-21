import { useState } from "react";
import { useAnnouncement } from "../hooks/useAdmin";
import { browserStorage } from "../storage";

// Stores the posted_at of the announcement the user dismissed.
const DISMISSED = "cc.dismissedAnnouncement";

export function AnnouncementBanner() {
  const announcement = useAnnouncement();
  const [dismissed, setDismissed] = useState(() => browserStorage()?.getItem(DISMISSED));
  const shown = announcement.data;

  if (!shown?.text || shown.posted_at === dismissed) return null;

  function dismiss(postedAt: string) {
    browserStorage()?.setItem(DISMISSED, postedAt);
    setDismissed(postedAt);
  }

  return (
    <div
      role="status"
      className="flex items-center gap-3 px-4 py-2 bg-yellow-200 border-b border-yellow-400 text-sm text-grey-dark"
    >
      <p className="flex-1">
        <span aria-hidden="true">📢 </span>
        {shown.text}
      </p>
      <button onClick={() => dismiss(shown.posted_at)} className="text-link">
        Dismiss
      </button>
    </div>
  );
}
