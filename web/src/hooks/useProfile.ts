import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  deleteKey,
  fetchAuthMethods,
  fetchProfile,
  fetchSiteKeypairs,
  fetchUsers,
  importKey,
  setDefaults,
  teardown,
} from "../api/client";
import type { Resource } from "../api/types";

export function useProfile() {
  return useQuery({ queryKey: ["profile"], queryFn: fetchProfile });
}

export function useSetDefaults() {
  const queries = useQueryClient();
  return useMutation({
    mutationFn: setDefaults,
    onSuccess: () => queries.invalidateQueries({ queryKey: ["profile"] }),
  });
}

export function useImportKey() {
  const queries = useQueryClient();
  return useMutation({
    mutationFn: ({ name, publicKey }: { name: string; publicKey: string }) =>
      importKey(name, publicKey),
    onSuccess: () => queries.invalidateQueries({ queryKey: ["profile"] }),
  });
}

export function useDeleteKey() {
  const queries = useQueryClient();
  return useMutation({
    mutationFn: ({ name, atSites }: { name: string; atSites: boolean }) =>
      deleteKey(name, atSites),
    onSuccess: () => queries.invalidateQueries({ queryKey: ["profile"] }),
  });
}

export function useRelease() {
  const queries = useQueryClient();
  return useMutation({
    mutationFn: ({ runId, resources }: { runId: string; resources: Resource[] }) =>
      teardown(runId, resources),
    onSuccess: () => {
      queries.invalidateQueries({ queryKey: ["profile"] });
      queries.invalidateQueries({ queryKey: ["threads"] });
    },
  });
}

export function useSiteKeypairs(site: string | null) {
  return useQuery({
    queryKey: ["keypairs", site],
    queryFn: () => fetchSiteKeypairs(site as string),
    enabled: Boolean(site),
    retry: false,
  });
}

export function useUsers(page: number, size: number | undefined) {
  return useQuery({
    queryKey: ["users", page, size],
    queryFn: () => fetchUsers(page, size),
    placeholderData: keepPreviousData,
  });
}

export function useAuthMethods() {
  return useQuery({ queryKey: ["auth-methods"], queryFn: fetchAuthMethods, staleTime: Infinity });
}
