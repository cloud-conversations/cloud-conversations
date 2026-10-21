import type { Message } from "../api/types";

export function AnswerMessage({ message }: { message: Message }) {
  const sources = message.payload.sources ?? [];
  return (
    <div className="bg-white border border-grey-light rounded-lg p-4">
      <p className="whitespace-pre-wrap text-sm leading-relaxed">{message.text}</p>
      {sources.length > 0 && (
        <div className="mt-3 pt-3 border-t border-grey-light">
          <p className="text-xs font-semibold text-grey uppercase tracking-wide mb-1">
            Documentation
          </p>
          <ul className="space-y-0.5">
            {sources.map((url) => (
              <li key={url}>
                <a
                  href={url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="text-sm text-link hover:underline break-all"
                >
                  {url}
                </a>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
