import {
  useMutation,
  useQueryClient,
  type QueryKey,
  type UseMutationOptions,
  type UseMutationResult,
} from "@tanstack/react-query";

import { notify, type NotifyInput } from "../ui/toast";
import { errorMessage } from "../utils";

/**
 * Thin wrapper over react-query's useMutation that folds in the two things
 * nearly every mutation on this app does by hand: invalidate a set of query
 * keys on success, and surface an error toast (via errorMessage) on
 * failure. It is deliberately opt-in — call sites with bespoke onSuccess logic
 * (dynamic messages, state resets, navigation) should keep using useMutation
 * directly. Anything passed via onSuccess/onError still runs; the built-in
 * invalidate/toast behavior runs alongside it.
 */
export interface UseApiMutationOptions<TData, TVariables>
  extends Omit<
    UseMutationOptions<TData, unknown, TVariables>,
    "mutationFn"
  > {
  mutationFn: (vars: TVariables) => Promise<TData>;
  /** Query keys to invalidate after a successful mutation. */
  invalidate?: QueryKey[];
  /** Success toast, static or derived from the result/variables. */
  successMessage?:
    | NotifyInput
    | ((data: TData, vars: TVariables) => NotifyInput);
  /** Toast title shown on error; the body comes from errorMessage(err, ...). */
  errorTitle?: string;
  /** Fallback body when the error carries no server message. */
  errorFallback?: string;
}

export function useApiMutation<TData = unknown, TVariables = void>({
  mutationFn,
  invalidate,
  successMessage,
  errorTitle,
  errorFallback,
  onSuccess,
  onError,
  ...rest
}: UseApiMutationOptions<TData, TVariables>): UseMutationResult<
  TData,
  unknown,
  TVariables
> {
  const queryClient = useQueryClient();

  return useMutation<TData, unknown, TVariables>({
    mutationFn,
    onSuccess: (data, vars, ...ctx) => {
      if (invalidate) {
        for (const key of invalidate) {
          void queryClient.invalidateQueries({ queryKey: key });
        }
      }
      if (successMessage) {
        notify(
          typeof successMessage === "function"
            ? successMessage(data, vars)
            : successMessage,
        );
      }
      onSuccess?.(data, vars, ...ctx);
    },
    onError: (err, vars, ...ctx) => {
      if (errorTitle || errorFallback) {
        notify({
          color: "red",
          title: errorTitle ?? "Error",
          message: errorMessage(err, errorFallback ?? errorTitle ?? "Request failed"),
        });
      }
      onError?.(err, vars, ...ctx);
    },
    ...rest,
  });
}
