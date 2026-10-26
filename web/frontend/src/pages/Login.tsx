import { useEffect, useState } from "react";
import { useNavigate, useLocation, Navigate } from "react-router";
import { Box, Button, Callout, Spinner, TextInput } from "@/ui/kit";
import AuthShell from "../components/AuthShell";
import { useAuth } from "../auth/AuthContext";
import { getAuthStatus, listPublicSsoProviders, ssoStartUrl } from "../api/client";
import type { SsoPublicProvider } from "../types";
import { safeLocalPath } from "../lib/safePath";

/** Human text for the short `?sso_error=` codes the SSO callback redirects with. */
const SSO_ERRORS: Record<string, string> = {
  state_expired: "The sign-in took too long or was started in another browser. Try again.",
  state_mismatch: "The sign-in response did not match this browser session. Try again.",
  unknown_provider: "That sign-in provider is not available.",
  not_configured: "Single sign-on is not configured: the server needs KOWAN_EXTERNAL_URL.",
  invalid_id_token: "The identity provider's response could not be verified.",
  token_exchange_failed: "The identity provider refused the sign-in code.",
  provider_unreachable: "The identity provider could not be reached.",
  signup_disabled: "No Kowan account is linked to this identity. Ask an admin to create or link one.",
  domain_not_allowed: "Your email domain is not allowed to sign in here.",
};

function ssoErrorText(code: string): string {
  if (code.startsWith("idp_")) return `The identity provider denied the sign-in (${code.slice(4)}).`;
  return SSO_ERRORS[code] ?? `Single sign-on failed (${code}).`;
}

interface LocationState {
  from?: { pathname?: string };
}

interface LoginValues {
  username: string;
  password: string;
}

export default function Login() {
  const auth = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const [submitting, setSubmitting] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);

  const [values, setValues] = useState<LoginValues>({ username: "", password: "" });
  const [errors, setErrors] = useState<Partial<Record<keyof LoginValues, string>>>({});
  // "checking" until /auth/status answers, so we never flash the login form on a
  // brand-new server that should go straight to first-run setup. On error we
  // fall back to "login" — the safe default when status can't be determined.
  const [phase, setPhase] = useState<"checking" | "login" | "setup">("checking");
  const [providers, setProviders] = useState<SsoPublicProvider[]>([]);
  const [ssoOnly, setSsoOnly] = useState(false);
  const [showPassword, setShowPassword] = useState(false);
  const ssoError = new URLSearchParams(location.search).get("sso_error");

  const dest = safeLocalPath((location.state as LocationState | null)?.from?.pathname);

  useEffect(() => {
    let cancelled = false;
    listPublicSsoProviders()
      .then((r) => {
        if (cancelled) return;
        setProviders(r.providers);
        setSsoOnly(r.ssoOnly);
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, []);

  // On a brand-new server there is no admin yet: send the operator to the
  // first-run setup screen instead of an unusable login form.
  useEffect(() => {
    let cancelled = false;
    getAuthStatus()
      .then((s) => !cancelled && setPhase(s.initialized ? "login" : "setup"))
      .catch(() => !cancelled && setPhase("login"));
    return () => {
      cancelled = true;
    };
  }, []);

  // If already authenticated, skip the login screen.
  // Use `dest`, not "/": right after a successful login this branch can render
  // before the transition-wrapped navigate(dest) below commits, and would
  // otherwise swallow the deep link the user came from.
  if (auth.token && !auth.loading) {
    return <Navigate to={dest} replace />;
  }
  if (phase === "setup") {
    return <Navigate to="/setup" replace />;
  }
  // Hold a spinner (not the login form) while auth is restoring or status is
  // still being checked — avoids a login flash before a redirect resolves.
  if (auth.loading || phase === "checking") {
    return (
      <Box align="center" justify="center" height="100vh">
        <Spinner />
      </Box>
    );
  }

  function validate(v: LoginValues): Partial<Record<keyof LoginValues, string>> {
    const next: Partial<Record<keyof LoginValues, string>> = {};
    if (v.username.trim().length === 0) next.username = "Username is required";
    if (v.password.length === 0) next.password = "Password is required";
    return next;
  }

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    const nextErrors = validate(values);
    setErrors(nextErrors);
    if (Object.keys(nextErrors).length > 0) return;

    setSubmitting(true);
    setFailure(null);
    try {
      await auth.login(values.username, values.password);
      navigate(dest, { replace: true });
    } catch (err) {
      const message =
        (err as { response?: { data?: { error?: string } } })?.response?.data?.error ??
        "Invalid username or password";
      setFailure(`${message}. Check the username and password and try again.`);
    } finally {
      setSubmitting(false);
    }
  };

  const passwordVisible = !ssoOnly || showPassword;

  return (
    <AuthShell
      title="Sign in"
      subtitle={ssoOnly ? "Sign in with your organisation's identity provider." : "Use your Kowan account. Ask an admin if you do not have one."}
      footer="Free and open source · MIT"
    >
      {ssoError && (
        <Box direction="column" style={{ marginBottom: 12 }}>
          <Callout color="danger">{ssoErrorText(ssoError)}</Callout>
        </Box>
      )}
      {providers.length > 0 && (
        <Box direction="column" gap={2} style={{ marginBottom: passwordVisible ? 16 : 0 }}>
          {providers.map((p) => (
            <Button
              key={p.id}
              variant={ssoOnly ? "solid" : "outline"}
              fullWidth
              iconLeft={p.icon || "key-outline-rounded"}
              href={ssoStartUrl(p.id, dest)}
            >
              Continue with {p.label}
            </Button>
          ))}
          {passwordVisible && (
            <div className="flex items-center gap-3 pt-3 text-[12px] text-muted-foreground">
              <span className="h-px flex-1 bg-border" />
              <span>or with a password</span>
              <span className="h-px flex-1 bg-border" />
            </div>
          )}
        </Box>
      )}
      {!passwordVisible && (
        <Box direction="column" style={{ marginTop: 12 }}>
          <Button variant="link" size="sm" onClick={() => setShowPassword(true)}>
            Break-glass sign-in with a local admin account
          </Button>
        </Box>
      )}
      {passwordVisible && <form onSubmit={handleSubmit} noValidate>
        <Box direction="column" gap={5}>
          <TextInput
            label="Username"
            autoComplete="username"
            autoFocus
            value={values.username}
            errorText={errors.username}
            onChange={(e) => setValues((v) => ({ ...v, username: e.target.value }))}
          />
          <TextInput
            type="password"
            label="Password"
            autoComplete="current-password"
            value={values.password}
            errorText={errors.password}
            onChange={(e) => setValues((v) => ({ ...v, password: e.target.value }))}
          />
          {failure && <Callout color="danger">{failure}</Callout>}
          <Button type="submit" fullWidth loading={submitting} variant={providers.length > 0 ? "outline" : "solid"}>
            Sign in
          </Button>
        </Box>
      </form>}
    </AuthShell>
  );
}
