import { useQuery } from "@tanstack/react-query";
import { fetchMe } from "../api/client";

export function useMe() {
  return useQuery({ queryKey: ["me"], queryFn: fetchMe });
}
