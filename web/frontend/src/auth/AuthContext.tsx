import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import { Navigate, useLocation } from "react-router";
import { useQueryClient } from "@tanstack/react-query";
import { Box, Spinner } from "@/ui/kit";
import * as apiClient from "../api/client";
import type { User } from "../types";

interface AuthContextValue {
  user: User | null;
  token: string | null;
  loading: boolean;
  login: (username: string, password: string) => Promise<void>;
  /** Adopt a session issued outside the password form (SSO ticket exchange). */
  adoptSession: (token: string, user: User) => void;
  logout: () => void;
}

const AuthContext = createContext<AuthContextValue | undefined>(undefined);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [token, setTokenState] = useState<string | null>(() => apiClient.getToken());
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
  const queryClient = useQueryClient();

  // On mount, restore the user via getMe() from the token in localStorage. SSO
  // sign-ins arrive through /login/sso (an HttpOnly one-time ticket redeemed
  // by POST), never as a token in the URL.
  useEffect(() => {
    let cancelled = false;

    const stored = apiClient.getToken();
    if (!stored) {
      setLoading(false);
      return;
    }
    apiClient
      .getMe()
      .then((u) => {
        if (!cancelled) {
          setUser(u);
          rememberUser(u);
        }
      })
      .catch((err) => {
        if (cancelled) return;
        const status = (err as { response?: { status?: number } })?.response?.status;
        if (status === 401 || status === 403) {
          // The server said no: the session is over.
          apiClient.clearToken();
          rememberUser(null);
          setTokenState(null);
          setUser(null);
        } else {
          // Offline or the server is restarting: keep the session and show the
          // shell with the last known identity; queries retry on reconnect.
          setUser(lastUser());
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const login = useCallback(async (username: string, password: string) => {
    const res = await apiClient.login(username, password);
    // Drop anything cached for a previous session (another user/role may have
    // been signed in in this tab) before the new identity takes effect.
    queryClient.clear();
    apiClient.setToken(res.token);
    setTokenState(res.token);
    setUser(res.user);
    rememberUser(res.user);
  }, [queryClient]);

  const adoptSession = useCallback((newToken: string, newUser: User) => {
    queryClient.clear();
    apiClient.setToken(newToken);
    setTokenState(newToken);
    setUser(newUser);
    rememberUser(newUser);
  }, [queryClient]);

  const logout = useCallback(() => {
    apiClient.revokeSession();
    apiClient.clearToken();
    rememberUser(null);
    setTokenState(null);
    setUser(null);
    // Cached lists (users, secrets, hosts…) must not survive into the next
    // session, which may belong to a less privileged user.
    queryClient.clear();
  }, [queryClient]);

  const value = useMemo<AuthContextValue>(
    () => ({ user, token, loading, login, adoptSession, logout }),
    [user, token, loading, login, adoptSession, logout],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

// The last signed-in identity (name and role, nothing secret), so the shell can
// render when the server cannot be reached at start-up.
const USER_KEY = "kowan_user";

function rememberUser(u: User | null): void {
  try {
    if (u) localStorage.setItem(USER_KEY, JSON.stringify({ id: u.id, username: u.username, role: u.role }));
    else localStorage.removeItem(USER_KEY);
  } catch {
    /* storage unavailable: nothing to remember */
  }
}

function lastUser(): User | null {
  try {
    const raw = localStorage.getItem(USER_KEY);
    return raw ? (JSON.parse(raw) as User) : null;
  } catch {
    return null;
  }
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) {
    throw new Error("useAuth must be used within an AuthProvider");
  }
  return ctx;
}

/** Wraps protected routes; redirects to /login when there is no token. */
export function RequireAuth({ children }: { children: ReactNode }) {
  const { token, loading } = useAuth();
  const location = useLocation();

  if (loading) {
    return (
      <Box align="center" justify="center" height="100vh">
        <Spinner />
      </Box>
    );
  }

  if (!token) {
    return <Navigate to="/login" replace state={{ from: location }} />;
  }

  return <>{children}</>;
}
