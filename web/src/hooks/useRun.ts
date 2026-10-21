import { useEffect, useRef } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { fetchRun, fetchRunEvents, teardown } from "../api/client";
import type { Resource, Run } from "../api/types";
import { isTerminal } from "../api/types";

export const RUN_POLL_MS = 3000;

function whileRunning(state: string | undefined): number | false {
  return state && isTerminal(state) ? false : RUN_POLL_MS;
}

/** The run, re-read while a job is working on it and once more when that ends. */
export function useRun(runId: string, releasing: boolean, building: boolean) {
  const queries = useQueryClient();
  const working = releasing || building;
  const was = useRef(working);

  useEffect(() => {
    if (was.current && !working) {
      queries.invalidateQueries({ queryKey: ["run", runId] });
      queries.invalidateQueries({ queryKey: ["run-events", runId] });
    }
    was.current = working;
  }, [working, runId, queries]);

  return useQuery({
    queryKey: ["run", runId],
    queryFn: () => fetchRun(runId),
    refetchInterval: (query) => {
      const run = query.state.data as Run | undefined;
      if (building || (releasing && run?.live_resources.length)) return RUN_POLL_MS;
      return whileRunning(run?.state);
    },
  });
}

export function useRunEvents(runId: string, state: string | undefined, building: boolean) {
  return useQuery({
    queryKey: ["run-events", runId],
    queryFn: () => fetchRunEvents(runId),
    refetchInterval: () => (building ? RUN_POLL_MS : whileRunning(state)),
  });
}

export function useTeardown(runId: string, threadId: string) {
  const queries = useQueryClient();
  return useMutation({
    mutationFn: (resources: Resource[]) => teardown(runId, resources),
    onSuccess: () => {
      queries.invalidateQueries({ queryKey: ["run", runId] });
      queries.invalidateQueries({ queryKey: ["thread", threadId] });
      queries.invalidateQueries({ queryKey: ["threads"] });
    },
  });
}
