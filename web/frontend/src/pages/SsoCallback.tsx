import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router";
import { Box, Button, Callout, Spinner } from "@/ui/kit";
import AuthShell from "../components/AuthShell";
import { useAuth } from "../auth/AuthContext";
import { ssoExchange } from "../api/client";
import { errorMessage } from "../utils";

/**
 * Landing page of a single sign-on round trip. The server's callback has set a
 * one-time HttpOnly ticket cookie (no token in any URL); redeem it for a
 * session and continue to the page the user started from.
 */
export default function SsoCallback() {
  const { adoptSession } = useAuth();
  const navigate = useNavigate();
  const [error, setError] = useState<string | null>(null);
  const started = useRef(false);

  useEffect(() => {
    // StrictMode mounts effects twice in dev; the ticket is single-use.
    if (started.current) return;
    started.current = true;
    ssoExchange()
      .then((res) => {
        adoptSession(res.token, res.user);
        navigate(res.returnTo || "/", { replace: true });
      })
      .catch((err) => setError(errorMessage(err, "Sign-in failed")));
  }, [adoptSession, navigate]);

  return (
    <AuthShell title="Signing you in" subtitle="Finishing single sign-on.">
      {error ? (
        <Box direction="column" gap={4}>
          <Callout color="danger">{error}</Callout>
          <Button fullWidth onClick={() => navigate("/login", { replace: true })}>
            Back to sign in
          </Button>
        </Box>
      ) : (
        <Box align="center" justify="center" style={{ minHeight: 80 }}>
          <Spinner />
        </Box>
      )}
    </AuthShell>
  );
}
