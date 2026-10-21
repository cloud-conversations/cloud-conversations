import { Link } from "react-router-dom";
import { useMe } from "../hooks/useMe";

export function WaitingBanner() {
  const waiting = useMe().data?.waiting_accounts ?? 0;

  if (waiting === 0) return null;

  return (
    <div
      role="status"
      className="flex items-center gap-3 px-4 py-2 bg-brand-info/20 border-b border-brand-info/40 text-sm text-grey-dark"
    >
      <p className="flex-1">
        {waiting === 1 ? "1 account is" : `${waiting} accounts are`} waiting for approval.
      </p>
      <Link to="/admin" className="text-link">
        Review
      </Link>
    </div>
  );
}
