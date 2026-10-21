import { ApiError } from "../api/client";

/** Why a call did not go through. */
export function said(error: unknown): string {
  if (error instanceof ApiError) {
    return error.detail || `the server answered ${error.status}`;
  }
  if (error instanceof Error) {
    return error.message || "the server could not be reached";
  }
  return "the server could not be reached";
}

export function Failed({ error }: { error: unknown }) {
  if (!error) return null;
  return (
    <p role="alert" className="text-sm text-brand-danger">
      That did not work: {said(error)}
    </p>
  );
}
