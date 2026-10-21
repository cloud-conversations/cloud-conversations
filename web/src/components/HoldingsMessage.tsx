import type { Message } from "../api/types";

export function HoldingsMessage({ message }: { message: Message }) {
  const tables = message.payload.tables ?? [];
  return (
    <div className="bg-white border border-grey-light rounded-lg p-4">
      <p className="text-sm leading-relaxed">{message.text}</p>
      {tables.map((table) => (
        <pre
          key={table}
          className="mt-3 text-xs font-mono bg-grey-lighter rounded p-3 overflow-x-auto"
        >
          {table}
        </pre>
      ))}
    </div>
  );
}
