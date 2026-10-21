import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { fetchSystemKeys, rotateSystemKey } from "../api/client";

export function useSystemKeys() {
  return useQuery({ queryKey: ["system-keys"], queryFn: fetchSystemKeys });
}

export function useRotateSystemKey() {
  const queries = useQueryClient();
  return useMutation({
    mutationFn: rotateSystemKey,
    onSuccess: () => queries.invalidateQueries({ queryKey: ["system-keys"] }),
  });
}
