import { useState } from "react";
import type { KeyChoice, Message } from "../api/types";
import { useProfile, useSetDefaults } from "../hooks/useProfile";
import { useSetKeypair } from "../hooks/useThread";
import { asDefault } from "../keypairs";
import { KeyChooser } from "./KeyChooser";

interface Props {
  message: Message;
  threadId: string;
  keypair: string | null;
  onSuggestion: (text: string) => void;
  onSaved: () => void;
}

export function NoticeMessage({
  message,
  threadId,
  keypair,
  onSuggestion,
  onSaved,
}: Props) {
  const suggestions = message.payload.suggestions ?? [];
  return (
    <div className="bg-brand-info/5 border border-brand-info/30 rounded-lg p-4">
      <p className="text-sm leading-relaxed">{message.text}</p>
      {suggestions.length > 0 && (
        <div className="mt-3 flex flex-wrap gap-2">
          {suggestions.map((suggestion) => (
            <button
              key={suggestion}
              onClick={() => onSuggestion(suggestion)}
              className="text-sm text-left px-2 py-1 rounded border border-grey-light bg-white hover:bg-grey-lighter"
            >
              {suggestion}
            </button>
          ))}
        </div>
      )}
      {message.payload.needs === "ssh_key_ref" &&
        (keypair ? (
          <p className="mt-3 text-sm text-grey">
            Using the keypair <span className="font-mono">{keypair}</span>.
          </p>
        ) : (
          <KeypairField threadId={threadId} onSaved={onSaved} />
        ))}
    </div>
  );
}

function KeypairField({
  threadId,
  onSaved,
}: {
  threadId: string;
  onSaved: () => void;
}) {
  const profile = useProfile();
  const saved = useSetKeypair(threadId);
  const defaults = useSetDefaults();
  const [makeDefault, setMakeDefault] = useState(false);

  function save(choice: KeyChoice) {
    saved.mutate(choice, {
      onSuccess: () => {
        if (makeDefault) defaults.mutate(asDefault(choice));
        onSaved();
      },
    });
  }

  return (
    <div className="mt-3">
      <KeyChooser save={save} saving={saved.isPending} error={saved.error ?? defaults.error} />
      {profile.data &&
        !profile.data.default_ssh_key_ref &&
        !profile.data.default_site_keypairs && (
        <label className="mt-2 flex items-center gap-1 text-sm">
          <input
            type="checkbox"
            checked={makeDefault}
            onChange={(e) => setMakeDefault(e.target.checked)}
          />
          Make default for new chats
        </label>
      )}
    </div>
  );
}
