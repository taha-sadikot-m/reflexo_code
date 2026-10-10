"use client";

import {
  useMutation,
  useQuery,
  useQueryClient,
  type QueryKey,
  type UseMutationOptions,
} from "@tanstack/react-query";
import { api } from "@/lib/api";

export function useAdminQuery<T>(
  queryKey: QueryKey,
  path: string,
  enabled = true,
) {
  return useQuery({
    queryKey,
    enabled,
    queryFn: ({ signal }) => api<T>(path, { signal }),
  });
}

type AdminRequest<TVariables> = (
  variables: TVariables,
) => { path: string; init?: RequestInit };

export function useAdminMutation<TData = unknown, TVariables = void>(
  request: AdminRequest<TVariables>,
  invalidate: QueryKey[] = [],
  options: Omit<
    UseMutationOptions<TData, Error, TVariables>,
    "mutationFn" | "onSuccess"
  > & { onSuccess?: (data: TData, variables: TVariables) => void | Promise<void> } = {},
) {
  const queryClient = useQueryClient();
  return useMutation<TData, Error, TVariables>({
    ...options,
    mutationFn: (variables) => {
      const { path, init } = request(variables);
      return api<TData>(path, init);
    },
    onSuccess: async (data, variables) => {
      await Promise.all(
        invalidate.map((queryKey) =>
          queryClient.invalidateQueries({ queryKey }),
        ),
      );
      await options.onSuccess?.(data, variables);
    },
  });
}
