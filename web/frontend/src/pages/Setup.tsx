import { useEffect, useState } from "react";
import { Navigate, useNavigate } from "react-router";
import { Box, Button, Spinner, TextInput } from "@/ui/kit";
import AuthShell from "../components/AuthShell";
import { useAuth } from "../auth/AuthContext";
import { getAuthStatus, initAdmin } from "../api/client";
import { notify } from "../ui/toast";

interface SetupValues {
  setupToken: string;
  username: string;
  password: string;
  confirm: string;
}

// First-run setup: when the control plane has no user yet, the
// operator creates the initial admin here instead of passing credentials via
// env. Once any user exists this screen bounces to /login.
export default function Setup() {
  const auth = useAuth();
  const navigate = useNavigate();

  const [checking, setChecking] = useState(true);
  const [initialized, setInitialized] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [values, setValues] = useState<SetupValues>({
    setupToken: "",
    username: "admin",
    password: "",
    confirm: "",
  });
  const [errors, setErrors] = useState<Partial<Record<keyof SetupValues, string>>>({});

  // Is the server already initialized? If so, there is nothing to set up.
  useEffect(() => {
    let cancelled = false;
    getAuthStatus()
      .then((s) => !cancelled && setInitialized(s.initialized))
      .catch(() => !cancelled && setInitialized(false))
      .finally(() => !cancelled && setChecking(false));
    return () => {
      cancelled = true;
    };
  }, []);

  if (auth.token && !auth.loading) return <Navigate to="/" replace />;
  if (!checking && initialized) return <Navigate to="/login" replace />;

  function validate(v: SetupValues): Partial<Record<keyof SetupValues, string>> {
    const next: Partial<Record<keyof SetupValues, string>> = {};
    if (v.setupToken.trim().length === 0) next.setupToken = "Paste the setup token from the server log";
    if (v.username.trim().length === 0) next.username = "Username is required";
    if (v.password.length < 8) next.password = "Use at least 8 characters";
    if (v.confirm !== v.password) next.confirm = "Passwords do not match";
    return next;
  }

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    const nextErrors = validate(values);
    setErrors(nextErrors);
    if (Object.keys(nextErrors).length > 0) return;

    setSubmitting(true);
    try {
      const username = values.username.trim();
      await initAdmin(username, values.password, values.setupToken.trim());
      // Sign straight in as the admin we just created.
      await auth.login(username, values.password);
      notify({
        color: "green",
        title: "Welcome to Kowan",
        message: "Administrator account created",
      });
      navigate("/", { replace: true });
    } catch (err) {
      const message =
        (err as { response?: { data?: { error?: string } } })?.response?.data?.error ??
        "Could not create the administrator account";
      notify({ color: "red", title: "Setup failed", message });
    } finally {
      setSubmitting(false);
    }
  };

  if (checking) {
    return (
      <Box align="center" justify="center" height="100vh">
        <Spinner />
      </Box>
    );
  }

  return (
    <AuthShell title="Create the administrator" subtitle="First run. This account can add more users and hosts later." footer="Free and open source · MIT">
      <form onSubmit={handleSubmit} noValidate>
        <Box direction="column" gap={5}>
          <TextInput
            label="Setup token"
            description="Printed in the server log at startup (docker logs kowan), and saved as setup-token in the data volume."
            autoComplete="off"
            spellCheck={false}
            autoFocus
            value={values.setupToken}
            errorText={errors.setupToken}
            onChange={(e) => setValues((v) => ({ ...v, setupToken: e.target.value }))}
          />
          <TextInput
            label="Username"
            autoComplete="username"
            value={values.username}
            errorText={errors.username}
            onChange={(e) => setValues((v) => ({ ...v, username: e.target.value }))}
          />
          <TextInput
            type="password"
            label="Password"
            description="At least 8 characters."
            autoComplete="new-password"
            value={values.password}
            errorText={errors.password}
            onChange={(e) => setValues((v) => ({ ...v, password: e.target.value }))}
          />
          <TextInput
            type="password"
            label="Confirm password"
            autoComplete="new-password"
            value={values.confirm}
            errorText={errors.confirm}
            onChange={(e) => setValues((v) => ({ ...v, confirm: e.target.value }))}
          />
          <Button type="submit" fullWidth loading={submitting}>
            Create administrator
          </Button>
        </Box>
      </form>
    </AuthShell>
  );
}
