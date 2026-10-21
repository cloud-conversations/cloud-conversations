import type { Source, Version } from "../api/types";
import { stamp } from "../time";

interface Props {
  version: Version;
  latest: number;
  experiment: number | null;
  buildInFlight: boolean;
  onApprove: (descriptionId: string, version: number) => void;
  onAnswer: (question: string) => void;
}

function Row({
  name,
  value,
  source,
}: {
  name: string;
  value: string;
  source: Source | null;
}) {
  return (
    <tr className="border-b border-grey-lighter">
      <th scope="row" className="text-left font-normal text-grey px-4 py-1 w-40">
        {name}
      </th>
      <td className="px-2 py-1 font-medium">{value}</td>
      <td className="px-4 py-1 text-xs text-grey">{source}</td>
    </tr>
  );
}

function shown(value: unknown): string {
  return typeof value === "string" ? value : JSON.stringify(value);
}

export function DescriptionCard({
  version,
  latest,
  experiment,
  buildInFlight,
  onApprove,
  onAnswer,
}: Props) {
  const { requirements: req } = version;
  const superseded = version.version < latest;
  const image = req.image;
  const support = image && `${image.supported ? "" : "not "}supported by ${image.testbed}`;
  const named = req.items.some((item) => item.key === "image_name" && item.value === image?.name);

  return (
    <div className="bg-white border border-grey-light rounded-lg overflow-hidden">
      <div className="px-4 py-2 bg-grey-lighter border-b border-grey-light flex items-baseline gap-2">
        <h3 className="text-sm font-semibold">
          {experiment ? `Experiment ${experiment}, version ` : "Version "}
          {version.version}
        </h3>
        <span className="text-xs text-grey">{req.site}</span>
        {superseded && (
          <span className="text-xs text-grey ml-auto">superseded by v{latest}</span>
        )}
      </div>

      <table className="w-full text-sm">
        <caption className="sr-only">Requirements</caption>
        <tbody>
          <Row
            name="lease length"
            value={`${req.duration_hours}h`}
            source={req.duration_source}
          />
          <Row
            name="lease start"
            value={
              req.start_at
                ? stamp(req.start_at)
                : "as soon as you approve the build"
            }
            source={req.start_source}
          />
          {req.items.map((item) => (
            <Row
              key={item.key}
              name={item.key}
              value={
                item.key === "image_name" && named
                  ? `${shown(item.value)}, ${support}`
                  : shown(item.value)
              }
              source={item.source}
            />
          ))}
          {image && !named && (
            <Row name="image" value={`${image.name}, ${support}`} source={null} />
          )}
        </tbody>
      </table>

      <div className="px-4 py-3 border-b border-grey-light">
        <p className="text-xs font-semibold text-grey uppercase tracking-wide mb-1">
          Rubric · every check must pass on the built host
        </p>
        <ul className="text-sm space-y-0.5">
          {version.rubric.map((check) => (
            <li key={check.id} className="font-mono text-xs">
              {check.kind} {check.comparator} {shown(check.expected)}
            </li>
          ))}
        </ul>
      </div>

      {version.open_questions.length > 0 && (
        <div className="px-4 py-3 border-b border-grey-light">
          <p className="text-xs font-semibold text-grey uppercase tracking-wide mb-1">
            Assumptions
          </p>
          <ul className="space-y-1">
            {version.open_questions.map((question) => (
              <li key={question} className="flex items-baseline gap-2 text-sm">
                <span className="flex-1">{question}</span>
                <button
                  onClick={() => onAnswer(question)}
                  className="text-xs text-link hover:underline flex-shrink-0"
                >
                  Change
                </button>
              </li>
            ))}
          </ul>
        </div>
      )}

      {version.unsupported.length > 0 && (
        <div className="px-4 py-3 border-b border-grey-light">
          <p className="text-xs font-semibold text-grey uppercase tracking-wide mb-1">
            This testbed cannot
          </p>
          <ul className="text-sm space-y-0.5">
            {version.unsupported.map((note) => (
              <li key={note}>{note}</li>
            ))}
          </ul>
        </div>
      )}

      <div className="px-4 py-3 flex items-center gap-3">
        <button
          onClick={() => onApprove(version.description_id, version.version)}
          disabled={buildInFlight}
          className={`px-3 py-1.5 rounded-md text-sm font-medium text-white disabled:opacity-40 ${
            superseded ? "bg-grey" : "bg-brand-success"
          }`}
        >
          Approve and build
        </button>
        <p className="text-xs text-grey">
          {buildInFlight
            ? "a build is already running on this experiment"
            : `Reserves the hardware above. If the first attempt fails the agent may look
               at the run, reserve different hardware, or revise the description.`}
        </p>
      </div>
    </div>
  );
}
