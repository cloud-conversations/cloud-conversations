import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  approve,
  confirmRelease,
  declineRelease,
  fetchThread,
  patchThread,
  postMessage,
  sendFeedback,
} from "../api/client";
import type { KeyChoice, ThreadDetail } from "../api/types";

export const THREAD_POLL_MS = 2000;

export function useThread(threadId: string | undefined) {
  return useQuery({
    queryKey: ["thread", threadId],
    queryFn: () => fetchThread(threadId as string),
    enabled: Boolean(threadId),
    refetchInterval: (query) =>
      (query.state.data as ThreadDetail | undefined)?.jobs.length ? THREAD_POLL_MS : false,
  });
}

function useThreadMutation<TArgs>(
  threadId: string,
  fn: (args: TArgs) => Promise<unknown>,
) {
  const queries = useQueryClient();
  return useMutation({
    mutationFn: fn,
    onSettled: () => {
      queries.invalidateQueries({ queryKey: ["thread", threadId] });
      queries.invalidateQueries({ queryKey: ["threads"] });
    },
  });
}

export function usePostMessage(threadId: string) {
  return useThreadMutation(threadId, (text: string) => postMessage(threadId, text));
}

export function useSendFeedback(threadId: string) {
  return useThreadMutation(threadId, (feedback: Parameters<typeof sendFeedback>[1]) =>
    sendFeedback(threadId, feedback),
  );
}

export function useSetKeypair(threadId: string) {
  return useThreadMutation(threadId, (choice: KeyChoice) => patchThread(threadId, choice));
}

export function useSetProject(threadId: string) {
  return useThreadMutation(threadId, (project: string) =>
    patchThread(threadId, { project_id: project }),
  );
}

export function useApprove(threadId: string) {
  return useThreadMutation(
    threadId,
    ({ descriptionId, version }: { descriptionId: string; version: number }) =>
      approve(threadId, descriptionId, version),
  );
}

export function useConfirmRelease(threadId: string) {
  return useThreadMutation(threadId, confirmRelease);
}

export function useDeclineRelease(threadId: string) {
  return useThreadMutation(threadId, declineRelease);
}
