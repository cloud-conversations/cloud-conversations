import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  clearAnnouncement,
  endSessions,
  fetchAnnouncement,
  fetchFeedback,
  fetchFeedbackDetail,
  fetchUser,
  patchUser,
  postAnnouncement,
} from "../api/client";

export function useUser(userId: string) {
  return useQuery({ queryKey: ["user", userId], queryFn: () => fetchUser(userId) });
}

function useAdminChange<T>(change: (arg: T) => Promise<unknown>, userId?: string) {
  const queries = useQueryClient();
  return useMutation({
    mutationFn: change,
    onSuccess: () => {
      for (const key of [["user", userId], ["users"], ["announcement"]]) {
        queries.invalidateQueries({ queryKey: key });
      }
    },
  });
}

export function usePatchUser(userId: string) {
  return useAdminChange((changes: object) => patchUser(userId, changes), userId);
}

export function useEndSessions(userId: string) {
  return useAdminChange(() => endSessions(userId), userId);
}

export function useFeedback(page: number, size: number | undefined) {
  return useQuery({
    queryKey: ["feedback", page, size],
    queryFn: () => fetchFeedback(page, size),
    placeholderData: keepPreviousData,
  });
}

export function useFeedbackDetail(feedbackId: string) {
  return useQuery({
    queryKey: ["feedback-detail", feedbackId],
    queryFn: () => fetchFeedbackDetail(feedbackId),
  });
}

export function useAnnouncement() {
  return useQuery({ queryKey: ["announcement"], queryFn: fetchAnnouncement });
}

export function usePostAnnouncement() {
  return useAdminChange(postAnnouncement);
}

export function useClearAnnouncement() {
  return useAdminChange(clearAnnouncement);
}
