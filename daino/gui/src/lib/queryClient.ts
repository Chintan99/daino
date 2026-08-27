// The app's single QueryClient.
//
// It lives outside the React tree as well as inside it, because menu commands
// invalidate cached server state from plain functions rather than from hooks.
import { QueryClient } from "@tanstack/react-query";

export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: 1,
      refetchOnWindowFocus: false,
      staleTime: 5_000,
    },
  },
});
