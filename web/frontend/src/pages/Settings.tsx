import { useRef, useState } from "react";
import { Box, Button, Callout, Divider, Icon, Text, TextInput, UploadArea } from "@/ui/kit";
import { useMutation } from "@tanstack/react-query";
import { changeOwnPassword, downloadBackup, restoreBackup } from "../api/client";
import { useAuth } from "../auth/AuthContext";
import PageHeader from "../components/PageHeader";
import SectionCard from "../components/SectionCard";
import { ThemePanel } from "../components/shell/ThemePicker";
import UpdatePanel from "../components/updates/UpdatePanel";
import { notify } from "../ui/toast";
import { errorMessage } from "../utils";

/** Extract a human-readable error message from an unknown error. */

// ---------------------------------------------------------------------------
// Profile: change own password
// ---------------------------------------------------------------------------

interface PasswordFormValues {
  password: string;
  confirm: string;
}

function ProfileCard() {
  const { user, adoptSession } = useAuth();

  const [values, setValues] = useState<PasswordFormValues>({
    password: "",
    confirm: "",
  });
  const [errors, setErrors] = useState<{ password?: string; confirm?: string }>({});

  const resetForm = () => {
    setValues({ password: "", confirm: "" });
    setErrors({});
  };

  const validate = (): boolean => {
    const next: { password?: string; confirm?: string } = {};
    if (values.password.length < 8) {
      next.password = "Password must be at least 8 characters";
    }
    if (values.confirm !== values.password) {
      next.confirm = "Passwords do not match";
    }
    setErrors(next);
    return Object.keys(next).length === 0;
  };

  const mutation = useMutation({
    mutationFn: (password: string) => {
      if (!user) throw new Error("Not signed in");
      return changeOwnPassword(user.id, password);
    },
    onSuccess: ({ user: updated, token }) => {
      // Other sessions end with the old password; this tab carries on.
      if (token) adoptSession(token, updated);
      notify({
        color: "success",
        title: "Password updated",
        message: "Your password was changed successfully.",
      });
      resetForm();
    },
    onError: (err) => {
      notify({
        color: "danger",
        title: "Failed to update password",
        message: errorMessage(err, "Could not update your password."),
      });
    },
  });

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (!validate()) return;
    mutation.mutate(values.password);
  };

  return (
    <SectionCard
      title="Profile"
      icon={<Icon name="lock-outline" size="sm" />}
      description="Change your account password."
    >
      <Text size="sm" color="muted">
        Signed in as{" "}
        <Text as="span" weight="semibold">
          {user?.username ?? "-"}
        </Text>
        {user?.role ? ` (${user.role})` : ""}.
      </Text>
      <form onSubmit={handleSubmit}>
        <Box direction="column" gap={4}>
          <TextInput
            type="password"
            label="New password"
            placeholder="New password"
            autoComplete="new-password"
            disabled={!user || mutation.isPending}
            value={values.password}
            onChange={(e) =>
              setValues((v) => ({ ...v, password: e.target.value }))
            }
            errorText={errors.password}
          />
          <TextInput
            type="password"
            label="Confirm new password"
            placeholder="Repeat new password"
            autoComplete="new-password"
            disabled={!user || mutation.isPending}
            value={values.confirm}
            onChange={(e) =>
              setValues((v) => ({ ...v, confirm: e.target.value }))
            }
            errorText={errors.confirm}
          />
          <Box direction="row" wrap justify="end">
            <Button color="info" type="submit" loading={mutation.isPending} disabled={!user}>
              Update password
            </Button>
          </Box>
        </Box>
      </form>
    </SectionCard>
  );
}

// ---------------------------------------------------------------------------
// Backup & restore (admin only)
// ---------------------------------------------------------------------------

function BackupCard() {
  const [file, setFile] = useState<File | null>(null);
  const resetKey = useRef(0);

  const downloadMutation = useMutation({
    mutationFn: () => downloadBackup(),
    onSuccess: () => {
      notify({
        color: "success",
        title: "Backup downloaded",
        message: "The configuration backup was downloaded.",
      });
    },
    onError: (err) => {
      notify({
        color: "danger",
        title: "Failed to download backup",
        message: errorMessage(err, "Could not download the backup."),
      });
    },
  });

  const restoreMutation = useMutation({
    mutationFn: (f: File) => restoreBackup(f),
    onSuccess: () => {
      notify({
        color: "success",
        title: "Backup restored",
        message: "Backup restored — restart the server to apply.",
      });
      setFile(null);
      // Bump the key to fully reset the UploadArea component.
      resetKey.current += 1;
    },
    onError: (err) => {
      notify({
        color: "danger",
        title: "Failed to restore backup",
        message: errorMessage(err, "Could not restore the backup."),
      });
    },
  });

  return (
    <SectionCard
      title="Backup & Restore"
      icon={<Icon name="database-outline" size="sm" />}
      description="Download a full configuration backup or restore from a previous one."
    >
      <Callout
        color="blue"
        variant="light"
        iconLeft={<Icon name="info-outline-rounded" size="sm" />}
        title="What is included"
      >
        The backup contains the database and the stacks data directory. Secrets
        are encrypted with <Text as="span" weight="semibold">KOWAN_SECRET_KEY</Text>, which is{" "}
        <Text as="span" weight="semibold">not</Text> included — restoring on another server
        requires the same key to decrypt them. A restore is staged and applied
        the next time the server starts.
      </Callout>

      <Divider />

      <Box direction="column" gap={2}>
        <Text size="sm" weight="semibold">
          Download backup
        </Text>
        <Text size="xs" color="muted">
          Save a date-stamped <Text as="span" mono>.tar.gz</Text> archive of
          the current configuration.
        </Text>
        <Box direction="row" wrap justify="end">
          <Button
            color="info"
            iconLeft="download-rounded"
            onClick={() => downloadMutation.mutate()}
            loading={downloadMutation.isPending}
          >
            Download backup
          </Button>
        </Box>
      </Box>

      <Divider />

      <Box direction="column" gap={2}>
        <Text size="sm" weight="semibold">
          Restore from backup
        </Text>
        <Text size="xs" color="muted">
          Upload a backup archive. The restore is staged and applied on the next
          server restart.
        </Text>
        <UploadArea
          key={resetKey.current}
          label="Select a .tar.gz backup"
          accept=".gz,.tar.gz,application/gzip"
          clearable
          value={file ? [file] : []}
          onChange={(files) => setFile(files[0] ?? null)}
          disabled={restoreMutation.isPending}
          icon={<Icon name="upload-rounded" size="sm" />}
        />
        <Box direction="row" wrap justify="end">
          <Button
            color="orange"
            iconLeft="upload-rounded"
            disabled={!file}
            loading={restoreMutation.isPending}
            onClick={() => {
              if (file) restoreMutation.mutate(file);
            }}
          >
            Restore backup
          </Button>
        </Box>
      </Box>
    </SectionCard>
  );
}


// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

export default function Settings() {
  const { user } = useAuth();
  const isAdmin = user?.role === "admin";

  return (
    <Box direction="column" gap={6}>
      <PageHeader
        title="Settings"
        description="Manage your profile and platform configuration."
        icon={<Icon name="settings-outline-rounded" size="lg" />}
      />
      <ProfileCard />
      <SectionCard title="Appearance" description="Palette and mode for this browser.">
        <div className="max-w-[420px]">
          <ThemePanel />
        </div>
      </SectionCard>
      {isAdmin && (
        <SectionCard title="Server updates" description="Update or roll back this control plane. Failed updates are rolled back automatically.">
          <UpdatePanel component="server" isAdmin={isAdmin} />
        </SectionCard>
      )}
      {isAdmin && <BackupCard />}
    </Box>
  );
}
