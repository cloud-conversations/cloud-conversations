import { useEffect, useRef } from "react";

interface Props {
  value: string;
  onChange: (value: string) => void;
  onSend: () => void;
  disabled: boolean;
  placeholder: string;
}

export function Composer({ value, onChange, onSend, disabled, placeholder }: Props) {
  const ref = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    if (!disabled) ref.current?.focus();
  }, [disabled]);

  return (
    <div className="p-3">
      <form
        className="flex gap-2 items-end"
        onSubmit={(e) => {
          e.preventDefault();
          if (value.trim() && !disabled) onSend();
        }}
      >
        <textarea
          ref={ref}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              if (value.trim() && !disabled) onSend();
            }
          }}
          rows={2}
          disabled={disabled}
          placeholder={placeholder}
          aria-label="Message"
          className="flex-1 text-sm border border-grey-light rounded-md p-2 resize-none focus:outline-hidden focus:ring-2 focus:ring-brand-info/40 disabled:bg-grey-lighter"
        />
        <button
          type="submit"
          disabled={disabled || !value.trim()}
          className="px-4 py-2 rounded-md bg-brand-info text-white text-sm font-medium disabled:opacity-40"
        >
          Send
        </button>
      </form>
      <p className="mt-1 text-xs text-grey">
        Don't paste passwords, tokens or private keys. Messages are saved and sent to the model.
      </p>
    </div>
  );
}
