import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  createThread,
  deleteThread,
  fetchThreads,
  fetchTrash,
  restoreThread,
} from "../api/client";
import { THREAD_POLL_MS } from "./useThread";

export function useThreads() {
  return useQuery({
    queryKey: ["threads"],
    queryFn: fetchThreads,
    refetchInterval: (query) =>
      query.state.data?.some((t) => t.activity === "running") ? THREAD_POLL_MS : false,
  });
}

export function useCreateThread() {
  const queries = useQueryClient();
  return useMutation({
    mutationFn: createThread,
    onSuccess: () => queries.invalidateQueries({ queryKey: ["threads"] }),
  });
}

export function useDeleteThread() {
  const queries = useQueryClient();
  return useMutation({
    mutationFn: deleteThread,
    onSuccess: () => {
      queries.invalidateQueries({ queryKey: ["threads"] });
      queries.invalidateQueries({ queryKey: ["trash"] });
    },
  });
}

export function useTrash() {
  return useQuery({ queryKey: ["trash"], queryFn: fetchTrash });
}

export function useRestoreThread() {
  const queries = useQueryClient();
  return useMutation({
    mutationFn: restoreThread,
    onSuccess: () => {
      queries.invalidateQueries({ queryKey: ["threads"] });
      queries.invalidateQueries({ queryKey: ["trash"] });
    },
  });
}
